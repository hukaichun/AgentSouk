"""A2A gateway: one Agent Card + one JSON-RPC endpoint per agent.

Agent Cards are served under a per-agent path prefix rather than at the
origin root — a deliberate deviation from A2A's single-agent-per-origin
assumption, since one souk fronts many agents at one origin.

Two ways to address an agent:
- `/a2a/id/{agent_id}/...` — the canonical, always-unambiguous route keyed
  by souk's own assigned id (see souk/schema.py's `agents.agent_id`).
- `/a2a/{name}/...` — the legacy, human-readable route, kept working for
  convenience: resolves transparently as long as exactly one currently-
  listed agent has that display name. `name` is not unique (multiple
  identities may register the same one — see repo.register_agents), so a
  collision here 404s/409s instead of silently picking a winner; a caller
  that needs to pin one specific agent (e.g. a sub-agent delegation config)
  should use the `id` route instead.

There is no separate task_id concept: A2A's `Task.id` (and every
`params.id` a `tasks/get`/`tasks/cancel` call addresses) is just this
run_id (see api_a2a._start_run's docstring for why real A2A's own
`params.id` on `tasks/send(Subscribe)` — a caller-chosen value — is
accepted but not used for anything, a deliberate deviation from strict
A2A interop). `params.contextId` (the real A2A field name — an earlier
version of this module used the pre-rename `sessionId`, a stale draft
name, not a souk invention either way) is thread_id, similarly always
database-generated (see repo.ensure_thread) — a caller-supplied
`contextId` only ever *reuses* one souk already issued, never mints a
new one under a caller-chosen name (an unrecognized one is a 404, not
silently created — see repo.ThreadNotFound). Omitting `contextId`
entirely, though, is the normal A2A first-contact case (the spec's own
"Agents MAY generate a new contextId...") and does mint a fresh one —
souk never requires a caller to have called `POST /threads` first (see
souk-no-forced-protocol-deviation). Real sub-agent delegation
(souk_agent_sdk.a2a_client) records lineage via `Message.
referenceTaskIds` (real A2A) but that never implies reusing an existing
child thread — `referenceTaskIds` is explicitly informational-only in
the spec, not a session-grouping primitive, so souk never uses it to
infer continuity (see repo.ensure_thread's docstring). A caller that
wants to continue talking to the same callee thread must pass back the
real `contextId` it was returned on the earlier call, same as any
other A2A session continuation.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from souk import repo
from souk.agui import build_run_agent_input
from souk.broker import drain_run, request_cancel
from souk.config import ServingSettings
from souk.core import Souk
from souk.deps import get_serving_settings, get_session, get_souk
from souk.grpc_server import make_handlers
from souk.identity import InvalidActorChain, verify_actor_chain
from souk.pause import is_resuming
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


def _build_agent_card(agent_id: str, agent: dict, public_http_url: str) -> dict:
    base = f"{public_http_url}/a2a/id/{agent_id}"
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
async def agent_card_by_id(
    agent_id: str,
    session: AsyncSession = Depends(get_session),
    serving: ServingSettings = Depends(get_serving_settings),
) -> dict:
    agent = await repo.get_agent_by_id(session, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' is not registered")
    return _build_agent_card(agent_id, agent, serving.public_http_url)


@router.get("/a2a/{name}/.well-known/agent.json")
async def agent_card_by_name(
    name: str,
    session: AsyncSession = Depends(get_session),
    serving: ServingSettings = Depends(get_serving_settings),
) -> dict:
    agent_id = await _resolve_agent_id(session, name)
    agent = await repo.get_agent_by_id(session, agent_id)
    return _build_agent_card(agent_id, agent, serving.public_http_url)


async def _start_run(
    session: AsyncSession, agent_id: str, params: dict, souk: Souk
) -> tuple[str, str, bool]:
    """Queues a run from A2A tasks/send(Subscribe) params. Returns
    (run_id, thread_id, is_new).

    A2A's Task.id is this run_id, not a separate id — see this module's
    docstring for why. `params.id`, if the caller sent one, is accepted
    (it's part of the JSON-RPC request shape) and simply ignored.

    is_new=False means either this session already had an active run (see
    repo.get_active_run_for_thread), or the target agent was already known
    to be offline at call time and the run was created pre-failed instead
    of queued (see the online check below) — either way nothing was
    enqueued, and the caller gets back a run whose current persisted state
    (possibly already terminal) is authoritative rather than something to
    wait on live. This is also what stops a caller from re-triggering an
    already-pending sub-agent task a second time (see souk/pause.py): a
    repeated tasks/send(Subscribe) on the same session becomes idempotent
    — same run_id back, current real state — rather than forking a second
    concurrent run on the same thread.
    """
    agent = await repo.get_agent_by_id(session, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' is not registered")

    context_id = params.get("contextId")
    metadata = params.get("metadata", {})
    # Real A2A (Message.referenceTaskIds — "a list of other task IDs
    # that this message references for additional context"), not a
    # souk invention: a caller delegating to a sub-agent (e.g. via
    # souk_agent_sdk.a2a_client) can reference its own current task id
    # (run_id) here, letting souk link the spawned thread back to the
    # caller's own thread for lineage (see repo.ensure_thread's
    # parent_thread_id and GET /threads/{root}/tree). Only the first
    # entry is used — souk's own use case never sends more than one.
    # Ignored by any A2A client that doesn't set it; a value souk doesn't
    # recognize (unknown/stale run_id) is treated the same as not having
    # sent one at all, not an error — this is informational context, not
    # a claim souk verifies.
    reference_task_ids = params.get("message", {}).get("referenceTaskIds") or []
    parent_thread_id = None
    if reference_task_ids:
        referenced_run = await repo.get_run(session, reference_task_ids[0])
        if referenced_run is not None:
            parent_thread_id = referenced_run["thread_id"]

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

    # `create_if_missing` defaults False here (unlike api_agui.py): A2A's
    # `contextId` is optional, so a caller that omits it entirely (the
    # normal first-contact case — see repo.ensure_thread) still gets a
    # fresh thread; but a caller that *does* supply one is claiming to
    # continue something specific, and an unrecognized one is a real
    # caller error (ThreadNotFound), not a request to create one under
    # that name.
    try:
        thread_id = await repo.ensure_thread(
            session, agent_id, context_id, parent_thread_id, metadata=metadata
        )
    except repo.ThreadNotFound as e:
        raise HTTPException(
            status_code=404, detail=f"thread '{e}' not found"
        ) from e
    except repo.ThreadOwnershipMismatch as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    active = await repo.get_active_run_for_thread(session, thread_id)
    # A2A never carries a resume — is_resuming(active, None) is always
    # False, so a paused (input-required) run reached via A2A can never
    # be bypassed here; see souk/pause.py's module docstring for why that's
    # deliberate. Whoever needs to actually resolve it does so on this
    # same thread_id, directly over this agent's own AG-UI endpoint.
    if active is not None and not is_resuming(active, None):
        return active["run_id"], thread_id, False
    resuming_run_id = active["run_id"] if active is not None else None

    messages = a2a_message_to_agui_messages(params.get("message", {}))
    resume_input = {"thread_id": thread_id, "messages": messages}

    if resuming_run_id is not None:
        # Reopens the *same* run_id for another round rather than
        # minting a new one — see repo.reopen_run's docstring for why a
        # stable identity across pause/resume rounds matters (it's what
        # lets a caller's A2A Task.id — this same run_id — stay valid
        # without ever needing to be retargeted).
        run_id = resuming_run_id
        starting_seq = await repo.get_last_event_seq(session, run_id)
        await repo.reopen_run(session, run_id, resume_input, metadata=metadata)
    else:
        created = await repo.create_run(session, thread_id, agent_id, "a2a", resume_input, metadata=metadata)
        run_id = created["run_id"]
        starting_seq = 0

    # append_thread_messages assigns each message its real, database-
    # generated id (discarding the placeholder a2a_message_to_agui_messages
    # set) and hands back the same messages with `id` set to that — this
    # exact return value is what goes to the provider below.
    messages = await repo.append_thread_messages(session, thread_id, run_id, messages)

    # Fast-fail (see souk.health's queued-timeout sweep for the fallback
    # covering the race where the target goes offline *after* this check):
    # if souk already knows the target is offline right now, don't queue
    # at all — mark the run failed immediately so the caller doesn't wait
    # out queued_timeout_seconds for something that was never going anywhere.
    if not repo.is_agent_online(agent["last_seen_at"], souk.settings.online_window_seconds):
        await repo.mark_run_status(
            session, run_id, "failed", metadata={"failureReason": "agent_offline"}
        )
        await session.commit()
        return run_id, thread_id, False

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

    souk.broker.enqueue_run(
        run_id, agent_id, thread_id, agui_input_json, "a2a", make_handlers(souk), seq=starting_seq
    )
    return run_id, thread_id, True


async def _finalize_delegated_call(session: AsyncSession, run_id: str) -> dict | None:
    """Runs after draining a call's (tasks/send or tasks/sendSubscribe)
    live output, to react to what actually happened rather than what the
    last translated AG-UI event claimed — see
    translate_a2a.agui_event_to_a2a_update: a raw RUN_FINISHED always
    maps to "completed" there, even when the real persisted status is
    'input-required' (the callee itself paused on an AG-UI interrupt —
    see souk/pause.py). Just reads back the real status; the delegating
    agent decides what to do with an honest "input-required" result
    (typically: report "still pending" and finish its own run normally
    — see providers/pydantic-ai-agent/pydantic_ai_agent/sub_agent_tool.py)
    — souk doesn't register any interest or subscription on its behalf.
    Whether a later call to the same callee gets a real answer or
    another "still pending" is decided fresh each time, purely by
    whether the callee's thread can currently accept a new run (see
    repo.get_active_run_for_thread) — nothing here needs to remember
    that an earlier call happened.
    """
    return await repo.get_run(session, run_id)


async def _rpc(
    agent_id: str, request: Request, session: AsyncSession, souk: Souk
) -> EventSourceResponse | dict:
    body = await request.json()
    method = body.get("method")
    params = body.get("params", {})
    rpc_id = body.get("id")

    if method == "tasks/send":
        run_id, thread_id, is_new = await _start_run(session, agent_id, params, souk)
        run = souk.broker.get(run_id) if is_new else None
        if run is not None:
            # No cleanup on early exit, deliberately: a caller
            # disconnecting mid-wait does not cancel the run — see
            # api_agui.run_agent's event_stream for why. The run's own
            # pipeline task forgets it from the registry once it
            # naturally finishes either way.
            events = [item async for item in drain_run(run)]
        else:
            # Not a fresh run: either already paused/finished, already
            # failed fast (see _start_run's offline check), or this is a
            # duplicate call racing a run that's live under a different
            # requester — either way, nothing to wait on here. Report its
            # current persisted state instead.
            events = await repo.get_run_events(session, run_id)
        db_run = await _finalize_delegated_call(session, run_id)
        display_name = await _display_name(session, agent_id)
        task = build_task(run_id, thread_id, display_name, db_run["status"] if db_run else "completed", events)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    if method == "tasks/sendSubscribe":
        run_id, thread_id, is_new = await _start_run(session, agent_id, params, souk)
        run = souk.broker.get(run_id) if is_new else None

        async def event_stream():
            if run is None:
                # Same "not fresh" situation as tasks/send above, but
                # streaming: emit one status update reflecting the
                # current persisted state and close — there's nothing
                # live to subscribe to.
                db_run = await _finalize_delegated_call(session, run_id)
                status = db_run["status"] if db_run else "completed"
                update = status_update_for_run_status(run_id, thread_id, status)
                yield {
                    "event": "message",
                    "data": json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": update}),
                }
                return
            # No cleanup on early exit, deliberately — see
            # api_agui.run_agent's event_stream for why a disconnected
            # caller does not cancel the run.
            async for item in drain_run(run):
                update = agui_event_to_a2a_update(item, run_id, thread_id)
                yield {
                    "event": "message",
                    "data": json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": update}),
                }
            db_run = await _finalize_delegated_call(session, run_id)
            if db_run is not None and db_run["status"] == "input-required":
                # Corrects the record: the loop above already sent
                # whatever the raw RUN_FINISHED event translated to
                # (always "completed", see agui_event_to_a2a_update) —
                # this final message overrides it with the real
                # persisted outcome, so a live watcher isn't left with a
                # false "completed" as the last word.
                final_update = status_update_for_run_status(run_id, thread_id, db_run["status"])
                yield {
                    "event": "message",
                    "data": json.dumps({"jsonrpc": "2.0", "id": rpc_id, "result": final_update}),
                }

        return EventSourceResponse(event_stream())

    if method == "tasks/get":
        # `params.id` is this run_id — A2A's Task.id is not a separate
        # concept here (see _start_run's docstring). Scoped to agent_id
        # too so a request against a different agent's endpoint can't
        # read a run that isn't actually this agent's.
        run_id = params.get("id")
        run = await repo.get_run(session, run_id)
        if run is None or run["agent_id"] != agent_id:
            return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": "task not found"}}
        events = await repo.get_run_events(session, run_id)
        display_name = await _display_name(session, agent_id)
        task = build_task(run_id, run["thread_id"], display_name, run["status"], events)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    if method == "tasks/cancel":
        run_id = params.get("id")
        db_run = await repo.get_run(session, run_id)
        if db_run is None or db_run["agent_id"] != agent_id:
            return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32001, "message": "task not found"}}
        # This is the only place in AG-UI/A2A that actually cancels a run
        # — a disconnected caller (tasks/sendSubscribe, AG-UI's
        # run_agent) never does, only this explicit request. broker.
        # request_cancel flips run.cancelled immediately (see its
        # docstring), then — if this run is still live on some
        # AgentSession connection — its own pipeline task writes the DB
        # status and tells the agent side to stop producing further
        # events for it (best-effort: a no-op if it already finished or
        # has no live connection). Not a synchronous wait for that DB
        # write to land — this response hardcodes "cancelled" regardless,
        # so there's nothing here that actually depends on it, just a
        # caller doing tasks/get immediately after could in principle
        # still observe the old status for a moment.
        run = souk.broker.get(run_id)
        if run is not None:
            request_cancel(run)
        events = await repo.get_run_events(session, run_id)
        display_name = await _display_name(session, agent_id)
        task = build_task(run_id, db_run["thread_id"], display_name, "cancelled", events)
        return {"jsonrpc": "2.0", "id": rpc_id, "result": task}

    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": f"method not found: {method}"}}


@router.post("/a2a/id/{agent_id}/rpc")
async def rpc_by_id(
    agent_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    souk: Souk = Depends(get_souk),
):
    return await _rpc(agent_id, request, session, souk)


@router.post("/a2a/{name}/rpc")
async def rpc_by_name(
    name: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    souk: Souk = Depends(get_souk),
):
    agent_id = await _resolve_agent_id(session, name)
    return await _rpc(agent_id, request, session, souk)
