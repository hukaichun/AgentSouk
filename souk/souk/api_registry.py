from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from souk import repo
from souk.core import Souk
from souk.deps import get_session, get_souk
from souk.identity import is_timestamp_fresh, issue_session_token, registration_signing_payload, verify_signature
from souk.models import AgentRosterEntry, RegisterBatchRequest, RegisterBatchResponse, RosterResponse

router = APIRouter()


async def _roster(session: AsyncSession, souk: Souk) -> list[AgentRosterEntry]:
    agents = await repo.list_agents(
        session,
        online_window_seconds=souk.settings.online_window_seconds,
        stale_hidden_window_seconds=souk.settings.stale_hidden_window_seconds,
    )
    return [AgentRosterEntry(**a) for a in agents]


@router.post("/agents/register", status_code=201)
async def register_agents(
    body: RegisterBatchRequest,
    session: AsyncSession = Depends(get_session),
    souk: Souk = Depends(get_souk),
) -> RegisterBatchResponse:
    if not is_timestamp_fresh(body.timestamp):
        raise HTTPException(status_code=401, detail="registration timestamp too far from souk's clock")
    payload = registration_signing_payload(body.sdk_client_id, [a.name for a in body.agents], body.timestamp)
    if not verify_signature(body.public_key, body.signature, payload):
        raise HTTPException(status_code=401, detail="invalid registration signature")

    agent_ids = await repo.register_agents(
        session,
        body.sdk_client_id,
        body.public_key,
        [agent.model_dump() for agent in body.agents],
        provider_name=body.provider_name,
    )

    return RegisterBatchResponse(
        agents=await _roster(session, souk),
        session_token=issue_session_token(body.sdk_client_id, souk.settings.token_signing_secret),
        agent_ids=agent_ids,
    )


@router.get("/agents")
async def list_agents(
    session: AsyncSession = Depends(get_session), souk: Souk = Depends(get_souk)
) -> RosterResponse:
    return RosterResponse(agents=await _roster(session, souk))
