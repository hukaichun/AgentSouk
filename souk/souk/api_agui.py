"""AG-UI HTTP surface: routes only.

What AG-UI *means* — minting a thread for an unrecognized threadId, deciding
whether a call starts a run or reports an active one, fast-failing an offline
agent — lives in souk/protocols/agui.py, in core. This file is the serving
half: it parses requests, turns adapter results into SSE or JSON, and maps
souk.errors onto status codes. Nothing here decides protocol semantics.

`POST /threads` remains an *optional* way to obtain a thread_id upfront —
e.g. to show it in a UI before the first message — not a prerequisite:
forcing every caller through it would break a standard, unmodified AG-UI
client that has never heard of it (souk-no-forced-protocol-deviation).
"""

import json

from ag_ui.core import RunAgentInput
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from souk.core import Souk
from souk.deps import get_souk
from souk.errors import AgentNotFound, AmbiguousAgentName, InvalidRunInput, ThreadOwnershipMismatch
from souk.identity import InvalidActorChain
from souk.models import CreateThreadRequest, CreateThreadResponse
from souk.protocols.agui import AGUIAdapter, ThreadSnapshot

router = APIRouter()


def _sse(event: dict) -> dict:
    """One AG-UI event as an SSE frame. The adapter yields events; framing
    them is this layer's job."""
    return {"event": "message", "data": json.dumps(event)}


def _name_conflict(exc: AmbiguousAgentName) -> HTTPException:
    """A display name is not exclusive across identities, so several matches
    is a normal outcome the caller has to resolve — answered with the
    candidates and the unambiguous route to retry against."""
    return HTTPException(
        status_code=409,
        detail={
            "error": f"multiple agents are registered under the name '{exc.name}'",
            "retry_with": "/agui/id/{agent_id} or /a2a/id/{agent_id}/...",
            "candidates": [
                {
                    "name": c["name"],
                    "agent_id": c["agent_id"],
                    "public_key_prefix": c["public_key"][:12],
                    "joined_at": c["joined_at"].isoformat(),
                    "description": c["agent_card"].get("description", ""),
                }
                for c in exc.candidates
            ],
        },
    )


async def _resolve(souk: Souk, name: str) -> str:
    try:
        return await AGUIAdapter(souk).resolve_agent_id(name)
    except AgentNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except AmbiguousAgentName as e:
        raise _name_conflict(e) from e


async def _create_thread(souk: Souk, agent_id: str, body: CreateThreadRequest) -> CreateThreadResponse:
    if await souk.get_agent(agent_id) is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' is not registered")
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
    return await _create_thread(souk, await _resolve(souk, name), body)


@router.get("/threads/{thread_id}")
async def get_thread_snapshot(thread_id: str, souk: Souk = Depends(get_souk)) -> dict:
    """Lets a caller catch up on a thread without a live stream — e.g. after
    its original AG-UI SSE connection closed because the run it was watching
    paused, and it needs to know what has happened since.
    """
    snapshot = await souk.get_thread_snapshot(thread_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"thread '{thread_id}' not found")
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
        raise HTTPException(status_code=404, detail=f"thread '{thread_id}' not found")
    return tree


async def _run_agent(souk: Souk, agent_id: str, body: RunAgentInput):
    try:
        result = await AGUIAdapter(souk).run(agent_id, body)
    except AgentNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ThreadOwnershipMismatch as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except InvalidActorChain as e:
        raise HTTPException(status_code=401, detail=f"invalid actor chain: {e}") from e
    except InvalidRunInput as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

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
        async for event in result.events:
            yield _sse(event)

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
    return await _run_agent(souk, await _resolve(souk, name), body)
