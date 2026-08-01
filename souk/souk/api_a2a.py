"""A2A gateway: one Agent Card + one JSON-RPC endpoint per agent.

Agent Cards are served under a per-agent path prefix
(`/a2a/{agent_name}/.well-known/agent.json`) rather than at the origin
root — a deliberate deviation from A2A's single-agent-per-origin
assumption, since one souk fronts many agents at one origin.

Per A2A's own protocol contract, `tasks/send(Subscribe)` callers supply
their own task id (`params.id`) and optional session id
(`params.sessionId`) — those are honored as given rather than replaced
with souk-assigned ids, since interop with real A2A clients depends on
the caller being able to correlate its own id with `tasks/get` later.
souk's own thread_id/run_id (used on the AG-UI side) stay souk-assigned.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from souk import repo
from souk.agui import build_run_agent_input
from souk.broker import END_OF_STREAM, broker
from souk.config import settings
from souk.db import get_session
from souk.translate_a2a import a2a_message_to_agui_messages, agui_event_to_a2a_update, build_task

router = APIRouter()


@router.get("/a2a/{agent_name}/.well-known/agent.json")
async def agent_card(agent_name: str, session: AsyncSession = Depends(get_session)) -> dict:
    agent = await repo.get_agent(session, agent_name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_name}' is not registered")
    base = f"{settings.public_http_url}/a2a/{agent_name}"
    card = dict(agent["agent_card"])
    return {
        "name": card.get("name", agent_name),
        "description": card.get("description", ""),
        "url": f"{base}/rpc",
        "version": "0.1.0",
        "capabilities": {"streaming": True},
        "skills": card.get("skills", []),
    }


async def _start_run(session: AsyncSession, agent_name: str, params: dict) -> tuple[str, str]:
    """Queues a run from A2A tasks/send(Subscribe) params. Returns (task_id, run_id)."""
    agent = await repo.get_agent(session, agent_name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_name}' is not registered")

    task_id = params.get("id")
    if not task_id:
        raise HTTPException(status_code=400, detail="params.id (task id) is required")
    session_id = params.get("sessionId")
    metadata = params.get("metadata", {})
    # Not part of core A2A — an extension field a caller can set (e.g. an
    # agent delegating to a sub-agent via souk_agent_sdk.a2a_client) to
    # link the spawned thread back to the caller's own thread. Ignored by
    # any A2A client that doesn't know about it.
    parent_thread_id = metadata.get("parentThreadId")

    try:
        thread_id = await repo.ensure_thread(
            session, agent_name, session_id, parent_thread_id, metadata=metadata
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    messages = a2a_message_to_agui_messages(params.get("message", {}))

    created = await repo.create_run(
        session,
        thread_id,
        agent_name,
        "a2a",
        {"thread_id": thread_id, "messages": messages},
        assign_task_id=False,
        metadata=metadata,
    )
    run_id = created["run_id"]

    try:
        agui_input_json = build_run_agent_input(thread_id, run_id, messages)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # tasks/send(Subscribe) task ids are caller-supplied (see module docstring),
    # so store it directly rather than souk's usual new_id("task").
    await repo.set_task_id(session, run_id, task_id)
    await repo.append_thread_messages(session, thread_id, run_id, messages)
    await session.commit()

    broker.enqueue_run(run_id, agent_name, thread_id, agui_input_json, "a2a")
    return task_id, run_id


@router.post("/a2a/{agent_name}/rpc")
async def rpc(agent_name: str, request: Request, session: AsyncSession = Depends(get_session)):
    body = await request.json()
    method = body.get("method")
    params = body.get("params", {})
    rpc_id = body.get("id")

    if method == "tasks/send":
        task_id, run_id = await _start_run(session, agent_name, params)
        state = broker.get(run_id)
        events: list[dict] = []
        while True:
            item = await state.output_queue.get()
            if item is END_OF_STREAM:
                break
            events.append(item)
        broker.forget(run_id)
        run = await repo.get_run(session, run_id)
        task = build_task(task_id, agent_name, run["status"] if run else "completed", events)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    if method == "tasks/sendSubscribe":
        task_id, run_id = await _start_run(session, agent_name, params)
        state = broker.get(run_id)

        async def event_stream():
            try:
                while True:
                    item = await state.output_queue.get()
                    if item is END_OF_STREAM:
                        break
                    update = agui_event_to_a2a_update(item, task_id)
                    yield {
                        "event": "message",
                        "data": json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": update}),
                    }
            finally:
                broker.forget(run_id)

        return EventSourceResponse(event_stream())

    if method == "tasks/get":
        task_id = params.get("id")
        run = await repo.get_run_by_task_id(session, task_id)
        if run is None:
            return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": "task not found"}}
        events = await repo.get_run_events(session, run["run_id"])
        task = build_task(task_id, agent_name, run["status"], events)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    if method == "tasks/cancel":
        task_id = params.get("id")
        run = await repo.get_run_by_task_id(session, task_id)
        if run is None:
            return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": "task not found"}}
        # Best-effort: marks the run cancelled so tasks/get reflects it; the
        # agent-side run in flight (if any) isn't forcibly interrupted in v1.
        await repo.mark_run_status(session, run["run_id"], "cancelled")
        events = await repo.get_run_events(session, run["run_id"])
        task = build_task(task_id, agent_name, "cancelled", events)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"method not found: {method}"}}
