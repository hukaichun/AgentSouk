from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from souk import repo
from souk.db import get_session
from souk.models import AgentRosterEntry, RegisterBatchRequest, RosterResponse

router = APIRouter()


@router.post("/agents/register", status_code=201)
async def register_agents(
    body: RegisterBatchRequest, session: AsyncSession = Depends(get_session)
) -> RosterResponse:
    await repo.register_agents(
        session,
        body.sdk_client_id,
        [agent.model_dump() for agent in body.agents],
    )
    agents = await repo.list_agents(session)
    return RosterResponse(agents=[AgentRosterEntry(**a) for a in agents])


@router.get("/agents")
async def list_agents(session: AsyncSession = Depends(get_session)) -> RosterResponse:
    agents = await repo.list_agents(session)
    return RosterResponse(agents=[AgentRosterEntry(**a) for a in agents])
