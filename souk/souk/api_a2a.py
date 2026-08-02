"""A2A gateway: one Agent Card + one JSON-RPC endpoint per agent.

Agent Cards are served under a per-agent path prefix rather than at the
origin root — a deliberate deviation from A2A's single-agent-per-origin
assumption, since one souk fronts many agents at one origin.

Two ways to address an agent:
- `/a2a/id/{agent_id}/...` — the canonical, always-unambiguous route keyed
  by souk's own assigned id (see souk/db.py's `agents.agent_id`).
- `/a2a/{name}/...` — the legacy, human-readable route, kept working for
  convenience: resolves transparently as long as exactly one currently-
  listed agent has that display name. `name` is not unique (multiple
  identities may register the same one — see repo.register_agents), so a
  collision here 404s/409s instead of silently picking a winner; a caller
  that needs to pin one specific agent (e.g. a sub-agent delegation config)
  should use the `id` route instead.

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
from souk.identity import InvalidActorChain, verify_actor_chain
from souk.translate_a2a import (
    a2a_message_to_agui_messages,
    agui_event_to_a2a_update,
    build_task,
    status_update_for_run_status,
)

router = APIRouter()


async def _resolve_agent_id(session: AsyncSession, name: str) -> str:
    """Resolves the legacy name-based routes down to a single agent_id, or
    raises 404 (no match) / 409 (ambiguous — more than one currently-listed
    agent shares this name) with enough in the body to retry against
    `/a2a/id/{agent_id}/...` or `/agui/id/{agent_id}`.
    """
    candidates = await repo.resolve_agents_by_name(session, name)
    if not candidates:
        raise HTTPException(status_code=404, detail=f"agent '{name}' is not registered")
    if len(candidates) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "error": f"multiple agents are registered under the name '{name}'",
                "retry_with": "/a2a/id/{agent_id}/... or /agui/id/{agent_id}",
                "candidates": [
                    {
                        "name": c["name"],
                        "agent_id": c["agent_id"],
                        "public_key_prefix": c["public_key"][:12],
                        "joined_at": c["joined_at"].isoformat(),
                        "description": c["agent_card"].get("description", ""),
                    }
                    for c in candidates
                ],
            },
        )
    return candidates[0]["agent_id"]


async def _display_name(session: AsyncSession, agent_id: str) -> str:
    agent = await repo.get_agent_by_id(session, agent_id)
    return agent["name"] if agent else agent_id


def _build_agent_card(agent_id: str, agent: dict) -> dict:
    base = f"{settings.public_http_url}/a2a/id/{agent_id}"
    card = dict(agent["agent_card"])
    return {
        "name": card.get("name", agent["name"]),
        "description": card.get("description", ""),
        "url": f"{base}/rpc",
        "version": "0.1.0",
        "capabilities": {"streaming": True},
        "skills": card.get("skills", []),
    }


@router.get("/a2a/id/{agent_id}/.well-known/agent.json")
async def agent_card_by_id(agent_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    agent = await repo.get_agent_by_id(session, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' is not registered")
    return _build_agent_card(agent_id, agent)


@router.get("/a2a/{name}/.well-known/agent.json")
async def agent_card_by_name(name: str, session: AsyncSession = Depends(get_session)) -> dict:
    agent_id = await _resolve_agent_id(session, name)
    agent = await repo.get_agent_by_id(session, agent_id)
    return _build_agent_card(agent_id, agent)


async def _start_run(session: AsyncSession, agent_id: str, params: dict) -> tuple[str, str, bool]:
    """Queues a run from A2A tasks/send(Subscribe) params.
    Returns (task_id, run_id, is_new).

    is_new=False means either this session already had an active run (see
    repo.get_active_run_for_thread), or the target agent was already known
    to be offline at call time and the run was created pre-failed instead
    of queued (see the online check below) — either way nothing was
    enqueued, and the caller gets back a run whose current persisted state
    (possibly already terminal) is authoritative rather than something to
    wait on live. This is also what stops a caller from re-triggering an
    already-pending sub-agent task a second time (see souk/pause.py): a
    repeated tasks/send(Subscribe) on the same session becomes idempotent
    — same task_id back, current real state — rather than forking a second
    concurrent run on the same thread.
    """
    agent = await repo.get_agent_by_id(session, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' is not registered")

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

    # Optional, opt-in caller identity: params.metadata.actorChain is an
    # ordered list of compact JWTs (see souk.identity.verify_actor_chain
    # and souk_agent_sdk.identity.new_actor_chain/extend_actor_chain) —
    # each hop signed by whoever performed it, chained together so the
    # whole path (and who it's ultimately on behalf of) can be verified
    # at once. Unsigned calls are still allowed (souk doesn't mandate
    # caller auth); a chain that's present but fails to verify is
    # rejected outright rather than silently treated as anonymous, since
    # that's more likely tampering than a caller that simply chose not
    # to send one.
    verified_subject = None
    verified_actors: list[dict] = []
    actor_chain = metadata.get("actorChain")
    if actor_chain:
        try:
            result = verify_actor_chain(actor_chain)
        except InvalidActorChain as e:
            raise HTTPException(status_code=401, detail=f"invalid actor chain: {e}") from e
        verified_subject = result.subject
        for public_key in result.actor_public_keys:
            resolved_actor_name = await repo.get_agent_name_for_public_key(session, public_key)
            verified_actors.append({"publicKey": public_key, "agentName": resolved_actor_name})
        metadata = {
            **metadata,
            "verifiedActorChain": {"subject": verified_subject, "actors": verified_actors},
        }

    try:
        thread_id = await repo.ensure_thread(
            session, agent_id, session_id, parent_thread_id, metadata=metadata
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
        agent_id,
        "a2a",
        {"thread_id": thread_id, "messages": messages},
        assign_task_id=False,
        metadata=metadata,
    )
    run_id = created["run_id"]
    if resuming_run_id is not None:
        await repo.mark_run_resumed(session, resuming_run_id, run_id)

    # tasks/send(Subscribe) task ids are caller-supplied (see module docstring),
    # so store it directly rather than souk's usual new_id("task").
    await repo.set_task_id(session, run_id, task_id)
    await repo.append_thread_messages(session, thread_id, run_id, messages)

    # Fast-fail (see souk.health's queued-timeout sweep for the fallback
    # covering the race where the target goes offline *after* this check):
    # if souk already knows the target is offline right now, don't queue
    # at all — mark the run failed immediately so the caller doesn't wait
    # out queued_timeout_seconds for something that was never going anywhere.
    if not repo.is_agent_online(agent["last_seen_at"]):
        await repo.mark_run_status(
            session, run_id, "failed", metadata={"failureReason": "agent_offline"}
        )
        await session.commit()
        return task_id, run_id, False

    # The raw chain (not just the resolved summary) is forwarded too — a
    # provider that wants to delegate further itself needs the actual
    # prior JWTs to extend the chain (see
    # souk_agent_sdk.identity.extend_actor_chain), not just souk's
    # human-readable summary of what it verified.
    forwarded_props = (
        {"caller": {"subject": verified_subject, "actors": verified_actors, "chain": actor_chain}}
        if verified_subject is not None
        else None
    )
    try:
        agui_input_json = build_run_agent_input(
            thread_id, run_id, messages, forwarded_props=forwarded_props
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    await session.commit()

    broker.enqueue_run(run_id, agent_id, thread_id, agui_input_json, "a2a")
    return task_id, run_id, True


async def _rpc(agent_id: str, request: Request, session: AsyncSession) -> EventSourceResponse | dict:
    body = await request.json()
    method = body.get("method")
    params = body.get("params", {})
    rpc_id = body.get("id")

    if method == "tasks/send":
        task_id, run_id, is_new = await _start_run(session, agent_id, params)
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
            # Not a fresh run: either already paused/finished, already
            # failed fast (see _start_run's offline check), or this is a
            # duplicate call racing a run that's live under a different
            # requester — either way, nothing to wait on here. Report its
            # current persisted state instead.
            events = await repo.get_run_events(session, run_id)
        run = await repo.get_run(session, run_id)
        display_name = await _display_name(session, agent_id)
        task = build_task(task_id, display_name, run["status"] if run else "completed", events)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    if method == "tasks/sendSubscribe":
        task_id, run_id, is_new = await _start_run(session, agent_id, params)
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
        display_name = await _display_name(session, agent_id)
        task = build_task(task_id, display_name, run["status"], events)
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
        display_name = await _display_name(session, agent_id)
        task = build_task(task_id, display_name, "cancelled", events)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"method not found: {method}"}}


@router.post("/a2a/id/{agent_id}/rpc")
async def rpc_by_id(agent_id: str, request: Request, session: AsyncSession = Depends(get_session)):
    return await _rpc(agent_id, request, session)


@router.post("/a2a/{name}/rpc")
async def rpc_by_name(name: str, request: Request, session: AsyncSession = Depends(get_session)):
    agent_id = await _resolve_agent_id(session, name)
    return await _rpc(agent_id, request, session)
