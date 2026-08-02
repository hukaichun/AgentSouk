from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from souk import repo
from souk.db import get_session
from souk.identity import is_timestamp_fresh, issue_session_token, registration_signing_payload, verify_signature
from souk.models import AgentRosterEntry, RegisterBatchRequest, RegisterBatchResponse, RosterResponse

router = APIRouter()


@router.post("/agents/register", status_code=201)
async def register_agents(
    body: RegisterBatchRequest, session: AsyncSession = Depends(get_session)
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

    agents = await repo.list_agents(session)
    return RegisterBatchResponse(
        agents=[AgentRosterEntry(**a) for a in agents],
        session_token=issue_session_token(body.sdk_client_id),
        agent_ids=agent_ids,
    )


@router.get("/agents")
async def list_agents(session: AsyncSession = Depends(get_session)) -> RosterResponse:
    agents = await repo.list_agents(session)
    return RosterResponse(agents=[AgentRosterEntry(**a) for a in agents])
