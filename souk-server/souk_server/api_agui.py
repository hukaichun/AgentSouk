"""AG-UI HTTP surface: routes only.

What AG-UI *means* — minting a thread for an unrecognized threadId, deciding
whether a call starts a run or reports an active one, fast-failing an offline
agent — lives in souk/protocols/agui.py, in core. This file parses requests
and frames results as SSE or JSON. It does not map errors either: adapters
raise souk.errors and one handler translates them for the whole app (see
souk.deps.install_error_handlers), because which status a failure deserves is
a property of the failure, not of the route that hit it.

`POST /threads` remains an *optional* way to obtain a thread_id upfront —
e.g. to show it in a UI before the first message — not a prerequisite:
forcing every caller through it would break a standard, unmodified AG-UI
client that has never heard of it (souk-no-forced-protocol-deviation).
"""

from ag_ui.core import RunAgentInput
from fastapi import APIRouter, Depends
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from souk.core import Souk
from souk_server.deps import get_souk
from souk.errors import AgentNotFound
from souk_server.models import CreateThreadRequest, CreateThreadResponse
from souk.protocols.agui import AGUIAdapter, ThreadSnapshot

router = APIRouter()


async def _create_thread(souk: Souk, agent_id: str, body: CreateThreadRequest) -> CreateThreadResponse:
    if await souk.get_agent(agent_id) is None:
        raise AgentNotFound(f"agent '{agent_id}' is not registered")
    return CreateThreadResponse(thread_id=await souk.create_thread(agent_id, metadata=body.metadata))


@router.post("/threads/id/{agent_id}")
async def create_thread_by_id(
    agent_id: str,
    body: CreateThreadRequest = CreateThreadRequest(),
    souk: Souk = Depends(get_souk),
) -> CreateThreadResponse:
    return await _create_thread(souk, agent_id, body)


@router.post("/threads/{name}")
async def create_thread_by_name(
    name: str,
    body: CreateThreadRequest = CreateThreadRequest(),
    souk: Souk = Depends(get_souk),
) -> CreateThreadResponse:
    agent_id = await AGUIAdapter(souk).resolve_agent_id(name)
    return await _create_thread(souk, agent_id, body)


@router.get("/threads/{thread_id}")
async def get_thread_snapshot(thread_id: str, souk: Souk = Depends(get_souk)) -> dict:
    """Lets a caller catch up on a thread without a live stream — e.g. after
    its original AG-UI SSE connection closed because the run it was watching
    paused, and it needs to know what has happened since.
    """
    snapshot = await souk.get_thread_snapshot(thread_id)
    if snapshot is None:
        raise AgentNotFound(f"thread '{thread_id}' not found")
    return snapshot


@router.get("/threads/{thread_id}/tree")
async def get_thread_tree(thread_id: str, souk: Souk = Depends(get_souk)) -> dict:
    """Full call-chain lineage rooted at `thread_id`, so whoever started the
    original call can later ask what their request actually fanned out to.
    Only as complete as callers chose to make it: a hop appears only if the
    caller recorded the lineage (real A2A `referenceTaskIds`, not a souk
    invention) when it called through souk.
    """
    tree = await souk.get_thread_tree(thread_id)
    if tree is None:
        raise AgentNotFound(f"thread '{thread_id}' not found")
    return tree


async def _run_agent(souk: Souk, agent_id: str, body: RunAgentInput):
    result = await AGUIAdapter(souk).run(agent_id, body)

    if isinstance(result, ThreadSnapshot):
        # The resolved thread_id is already the top-level `thread_id` field
        # of this body — the standard in-band place for it, so no custom
        # header is needed.
        return JSONResponse(jsonable_encoder(result.data))

    # No X-Souk-Thread-Id/X-Souk-Run-Id headers either: a run's own first
    # event is RUN_STARTED, which every compliant AG-UI provider emits with
    # threadId/runId copied from the RunAgentInput it was given. That is the
    # standard, in-band place a client learns them.
    async def stream():
        async for data in result.encode():
            yield {"event": "message", "data": data}

    return EventSourceResponse(stream())


@router.post("/agui/id/{agent_id}", response_model=None)
async def run_agent_by_id(
    agent_id: str, body: RunAgentInput, souk: Souk = Depends(get_souk)
) -> EventSourceResponse | JSONResponse:
    return await _run_agent(souk, agent_id, body)


@router.post("/agui/{name}", response_model=None)
async def run_agent_by_name(
    name: str, body: RunAgentInput, souk: Souk = Depends(get_souk)
) -> EventSourceResponse | JSONResponse:
    agent_id = await AGUIAdapter(souk).resolve_agent_id(name)
    return await _run_agent(souk, agent_id, body)
