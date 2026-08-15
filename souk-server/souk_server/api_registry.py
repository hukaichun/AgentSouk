"""Registration and roster HTTP surface: routes only.

Verifying that a registration really holds the key it claims is domain, not
HTTP — the same act for a provider across a network and one in this process —
so it lives on `Souk` (see `Souk.register_agents`). This file only parses the
request; a rejection becomes a 401 through the app-wide handler.
"""

from fastapi import APIRouter, Depends

from souk.core import Souk
from souk_server.deps import get_souk
from souk_server.models import AgentRosterEntry, RegisterBatchRequest, RegisterBatchResponse, RosterResponse

router = APIRouter()


@router.post("/agents/register", status_code=201)
async def register_agents(
    body: RegisterBatchRequest, souk: Souk = Depends(get_souk)
) -> RegisterBatchResponse:
    registration = await souk.register_agents(
        body.public_key,
        body.signature,
        body.timestamp,
        [agent.model_dump() for agent in body.agents],
        provider_name=body.provider_name,
    )

    return RegisterBatchResponse(
        agents=[AgentRosterEntry(**a) for a in await souk.list_agents()],
        session_token=registration.session_token,
        agent_ids=registration.agent_ids,
    )


@router.get("/agents")
async def list_agents(souk: Souk = Depends(get_souk)) -> RosterResponse:
    return RosterResponse(agents=[AgentRosterEntry(**a) for a in await souk.list_agents()])
