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
from souk.identity import a2a_call_signing_payload, verify_signature
from souk.translate_a2a import (
    a2a_message_to_agui_messages,
    agui_event_to_a2a_update,
    build_task,
    status_update_for_run_status,
)

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


async def _start_run(session: AsyncSession, agent_name: str, params: dict) -> tuple[str, str, bool]:
    """Queues a run from A2A tasks/send(Subscribe) params.
    Returns (task_id, run_id, is_new).

    is_new=False means this session already had an active run (see
    repo.get_active_run_for_thread) and nothing new was queued — the
    caller gets back the *existing* task_id/run_id instead, which may
    already be paused ('input-required') or even finished by the time
    they look at it. This is what stops a caller from re-triggering an
    already-pending sub-agent task a second time (see souk/pause.py):
    a repeated tasks/send(Subscribe) on the same session becomes
    idempotent — same task_id back, current real state — rather than
    forking a second concurrent run on the same thread.
    """
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

    # Optional, opt-in caller identity: a caller that holds an Ed25519 key
    # (e.g. a provider signing with the same identity it registered
    # with — see providers/pydantic-ai-agent's sub-agent tool) can prove
    # who it is by including callerPublicKey/callerSignature. Unsigned
    # calls are still allowed (souk doesn't mandate caller auth — see
    # souk/identity.py's a2a_call_signing_payload docstring); a signature
    # that's present but doesn't verify is rejected outright rather than
    # silently treated as anonymous, since that's more likely tampering
    # than a legitimate caller who simply chose not to sign.
    verified_caller_name = None
    caller_public_key = metadata.get("callerPublicKey")
    caller_signature = metadata.get("callerSignature")
    if caller_public_key and caller_signature:
        payload = a2a_call_signing_payload(task_id, session_id)
        if not verify_signature(caller_public_key, caller_signature, payload):
            raise HTTPException(status_code=401, detail="invalid caller signature")
        verified_caller_name = await repo.get_agent_name_for_public_key(session, caller_public_key)
        metadata = {
            **metadata,
            "verifiedCaller": {"agentName": verified_caller_name, "publicKey": caller_public_key},
        }

    try:
        thread_id = await repo.ensure_thread(
            session, agent_name, session_id, parent_thread_id, metadata=metadata
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    active = await repo.get_active_run_for_thread(session, thread_id)
    if active is not None and not (metadata.get("resume") and active["status"] == "input-required"):
        return active["task_id"] or task_id, active["run_id"], False
    resuming_run_id = active["run_id"] if active is not None else None

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
    if resuming_run_id is not None:
        await repo.mark_run_resumed(session, resuming_run_id, run_id)

    forwarded_props = (
        {"caller": {"agentName": verified_caller_name, "publicKey": caller_public_key}}
        if verified_caller_name
        else None
    )
    try:
        agui_input_json = build_run_agent_input(
            thread_id, run_id, messages, forwarded_props=forwarded_props
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    # tasks/send(Subscribe) task ids are caller-supplied (see module docstring),
    # so store it directly rather than souk's usual new_id("task").
    await repo.set_task_id(session, run_id, task_id)
    await repo.append_thread_messages(session, thread_id, run_id, messages)
    await session.commit()

    broker.enqueue_run(run_id, agent_name, thread_id, agui_input_json, "a2a")
    return task_id, run_id, True


@router.post("/a2a/{agent_name}/rpc")
async def rpc(agent_name: str, request: Request, session: AsyncSession = Depends(get_session)):
    body = await request.json()
    method = body.get("method")
    params = body.get("params", {})
    rpc_id = body.get("id")

    if method == "tasks/send":
        task_id, run_id, is_new = await _start_run(session, agent_name, params)
        state = broker.get(run_id) if is_new else None
        if state is not None:
            events: list[dict] = []
            while True:
                item = await state.output_queue.get()
                if item is END_OF_STREAM:
                    break
                events.append(item)
            broker.forget(run_id)
        else:
            # Not a fresh run: either already paused/finished, or this is
            # a duplicate call racing a run that's live under a different
            # requester — either way, nothing to wait on here. Report
            # its current persisted state instead (see _start_run).
            events = await repo.get_run_events(session, run_id)
        run = await repo.get_run(session, run_id)
        task = build_task(task_id, agent_name, run["status"] if run else "completed", events)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    if method == "tasks/sendSubscribe":
        task_id, run_id, is_new = await _start_run(session, agent_name, params)
        state = broker.get(run_id) if is_new else None

        async def event_stream():
            if state is None:
                # Same "not fresh" situation as tasks/send above, but
                # streaming: emit one status update reflecting the
                # current persisted state and close — there's nothing
                # live to subscribe to.
                run = await repo.get_run(session, run_id)
                status = run["status"] if run else "completed"
                update = status_update_for_run_status(task_id, status)
                yield {
                    "event": "message",
                    "data": json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": update}),
                }
                return
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
