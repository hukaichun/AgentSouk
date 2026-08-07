"""AG-UI gateway: `POST /agui/...` runs an agent, minting a fresh
thread_id automatically the first time a given (or omitted) `threadId`
isn't recognized — see _run_agent's use of `create_if_missing=True` and
souk-no-forced-protocol-deviation (a standard AG-UI client that has
never heard of `POST /threads` must still work). `POST /threads` (see
create_thread below) remains available as an *optional* way to obtain a
thread_id upfront — e.g. to show it in a UI before the first message —
not a required prerequisite.

The inbound `RunAgentInput` here is the real `ag_ui.core.RunAgentInput`,
not a souk-specific reimplementation — see souk/models.py's module
docstring for why there used to be one and isn't anymore.
"""

import json

from ag_ui.core import RunAgentInput
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from souk import repo
from souk.agui import build_run_agent_input, rewrite_message_ids
from souk.broker import broker, drain_run
from souk.grpc_server import HANDLERS
from souk.db import get_session
from souk.kyok import issue_kyok_token
from souk.models import CreateThreadRequest, CreateThreadResponse
from souk.pause import is_resuming

router = APIRouter()


async def _create_thread(agent_id: str, body: CreateThreadRequest, session: AsyncSession) -> CreateThreadResponse:
    """An *optional* way to obtain a thread_id upfront (e.g. to show it in
    a UI before the first message) — not required. `/agui`/`/a2a` both
    mint one automatically on first contact if the caller doesn't have
    one yet (see repo.ensure_thread's `create_if_missing` — AG-UI — and
    its no-id-at-all fallback — A2A's optional `contextId`); forcing
    every caller through this endpoint first would mean a standard,
    unmodified AG-UI/A2A client that's never heard of it stops working,
    which souk-no-forced-protocol-deviation rules out.
    """
    agent = await repo.get_agent_by_id(session, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' is not registered")
    thread_id = await repo.create_thread(session, agent_id, metadata=body.metadata)
    await session.commit()
    return CreateThreadResponse(thread_id=thread_id)


@router.post("/threads/id/{agent_id}")
async def create_thread_by_id(
    agent_id: str, body: CreateThreadRequest = CreateThreadRequest(), session: AsyncSession = Depends(get_session)
) -> CreateThreadResponse:
    return await _create_thread(agent_id, body, session)


@router.post("/threads/{name}")
async def create_thread_by_name(
    name: str, body: CreateThreadRequest = CreateThreadRequest(), session: AsyncSession = Depends(get_session)
) -> CreateThreadResponse:
    agent_id = await _resolve_agent_id(session, name)
    return await _create_thread(agent_id, body, session)


async def _resolve_agent_id(session: AsyncSession, name: str) -> str:
    """See api_a2a._resolve_agent_id — same legacy name-route resolution,
    duplicated rather than shared across modules since it's small and each
    HTTP surface owns its own request/response translation.
    """
    candidates = await repo.resolve_agents_by_name(session, name)
    if not candidates:
        raise HTTPException(status_code=404, detail=f"agent '{name}' is not registered")
    if len(candidates) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "error": f"multiple agents are registered under the name '{name}'",
                "retry_with": "/agui/id/{agent_id} or /a2a/id/{agent_id}/...",
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


@router.get("/threads/{thread_id}")
async def get_thread_snapshot(thread_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Lets a caller catch up on a thread without a live stream — e.g.
    after its original AG-UI SSE connection closed because the run it was
    watching paused (status='input-required'), and it needs to know
    whether/what has happened since. See repo.get_thread_snapshot.
    """
    snapshot = await repo.get_thread_snapshot(session, thread_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"thread '{thread_id}' not found")
    return snapshot


@router.get("/threads/{thread_id}/tree")
async def get_thread_tree(thread_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """Full call-chain lineage rooted at `thread_id`: itself plus every
    descendant thread spawned from it (recursively, via
    threads.parent_thread_id — see repo.get_thread_children), so whoever
    started the original call can later ask "what did my request actually
    fan out to." Only as complete as callers chose to make it: a hop only
    appears if the caller set Message.referenceTaskIds (real A2A, not a
    souk invention) when it called through souk (see api_a2a._start_run)
    — souk can't discover this on its own for calls that didn't opt in.
    """
    root = await repo.get_thread(session, thread_id)
    if root is None:
        raise HTTPException(status_code=404, detail=f"thread '{thread_id}' not found")

    async def build(node_thread_id: str) -> dict:
        children = await repo.get_thread_children(session, node_thread_id)
        return {
            "thread_id": node_thread_id,
            "children": [
                {**child, "children": (await build(child["thread_id"]))["children"]}
                for child in children
            ],
        }

    tree = await build(thread_id)
    return {"thread_id": thread_id, "agent_id": root["agent_id"], "children": tree["children"]}


def _build_forwarded_props(
    run_id: str, agent_id: str, metadata: dict, caller_forwarded_props: object
) -> object:
    """Keep Your Own Key opt-in: a caller that's running its own KYOK
    bridge (see souk.api_llm_bridge / souk-client-sdk's KyokBridge) passes
    `metadata.kyok.sessionId` — the session it's already long-polling
    `/kyok/poll` for — when starting the run. If present, mint a
    run-scoped token binding this run_id (and the agent_id it's assigned
    to — souk already knows this at mint time, so the token says so
    explicitly rather than leaving it implicit; see api_llm_bridge.
    chat_completions for where that's checked), merged into whatever
    forwardedProps the caller itself already supplied (own app-specific
    context is real AG-UI usage too, now that the real RunAgentInput
    schema is used directly — see this module's docstring — so KYOK must
    not clobber it). A provider that doesn't look for forwardedProps.kyok
    just never sees it and calls its own configured LLM as always — KYOK
    is opt-in on both the caller's and the provider's side independently,
    souk never forces either.

    Deliberately just the token, not a baseUrl too: settings.
    public_http_url is what *external* callers (browsers,
    souk-client-sdk from the host) use to reach souk, and is often not
    reachable at all from inside a provider's own container/network (see
    docker-compose.yml — providers reach souk at `http://souk:8000`, not
    `http://localhost:8000`). A provider already knows how it reaches
    souk (it's the same souk_http_url it registers/polls/calls A2A
    against) — see providers/pydantic-ai-agent's resolve_kyok_model,
    which builds `{souk_http_url}/kyok/v1` itself instead of trusting a
    value souk would otherwise have to guess.
    """
    session_id = metadata.get("kyok", {}).get("sessionId") if isinstance(metadata.get("kyok"), dict) else None
    if not session_id:
        return caller_forwarded_props
    kyok = {"kyok": {"token": issue_kyok_token(run_id, session_id, agent_id)}}
    if isinstance(caller_forwarded_props, dict):
        return {**caller_forwarded_props, **kyok}
    return kyok


async def _run_agent(
    agent_id: str, body: RunAgentInput, session: AsyncSession
) -> EventSourceResponse | JSONResponse:
    """`body` is the real `ag_ui.core.RunAgentInput` — not a souk-specific
    reimplementation (see souk/models.py's module docstring for why there
    used to be one and isn't anymore). `body.run_id`, whatever the caller
    sent, is never used for anything — souk always mints its own; the
    field is only present because the real schema requires it.
    """
    agent = await repo.get_agent_by_id(session, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' is not registered")

    # Not a declared field on ag_ui.core.RunAgentInput (extra="allow" —
    # see that model), so it's absent rather than defaulted when the
    # caller doesn't send one.
    metadata = getattr(body, "metadata", None) or {}
    resume = [r.model_dump(mode="json", by_alias=True) for r in body.resume] if body.resume else None

    # AG-UI's `threadId` is a field the *caller* mints locally (it's
    # required by the real schema) and there's no separate "create
    # thread" concept in AG-UI itself — an id souk hasn't seen before is
    # indistinguishable from "this is a brand new conversation", not a
    # caller error. create_if_missing=True mints a real, souk-generated
    # thread_id in that case (discarding the caller's own string, same
    # as any other caller-supplied id) rather than 404ing — see
    # repo.ensure_thread's docstring and souk-no-forced-protocol-deviation:
    # a standard AG-UI client that has never heard of `POST /threads`
    # must still work.
    try:
        thread_id = await repo.ensure_thread(
            session, agent_id, body.thread_id, metadata=metadata, create_if_missing=True
        )
    except repo.ThreadOwnershipMismatch as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    # A thread only ever has one active run at a time (see
    # repo.get_active_run_for_thread) — starting a second one
    # concurrently would fork its otherwise-linear history with no
    # clean way to merge it back. Instead of erroring or silently
    # queueing a duplicate, hand back the thread's current state
    # (including the pending run's real status) so the caller can act
    # on it — this doubles as the "catch me up" path for a run that
    # has since paused. (A freshly-minted thread_id from just above
    # naturally has no active run yet, so this is a no-op for it.)
    active = await repo.get_active_run_for_thread(session, thread_id)
    if active is not None and not is_resuming(active, resume):
        # No X-Souk-Thread-Id header here — the resolved thread_id is
        # already the top-level `thread_id` field in this JSON body (see
        # repo.get_thread_snapshot), the standard in-band place for it.
        snapshot = await repo.get_thread_snapshot(session, thread_id)
        return JSONResponse(jsonable_encoder(snapshot))
    resuming_run_id = active["run_id"] if active is not None else None

    input_dump = body.model_dump(mode="json", by_alias=True)

    if resuming_run_id is not None:
        # Reopens the *same* run_id for another round rather than
        # minting a new one — see repo.reopen_run's docstring for why a
        # stable identity across pause/resume rounds matters (it's what
        # lets this run's own A2A Task.id, if it has one, stay valid
        # without ever needing to be retargeted).
        run_id = resuming_run_id
        starting_seq = await repo.get_last_event_seq(session, run_id)
        await repo.reopen_run(session, run_id, input_dump, metadata=metadata)
    else:
        created = await repo.create_run(session, thread_id, agent_id, "ag-ui", input_dump, metadata=metadata)
        run_id = created["run_id"]
        starting_seq = 0

    # append_thread_messages assigns each message its real, database-
    # generated id (discarding whatever id, if any, the caller sent) and
    # hands back the same messages with `id` set to that — this exact
    # return value (not body.messages) is what goes to the provider below.
    raw_messages = [m.model_dump(mode="json", by_alias=True) for m in body.messages]
    messages = await repo.append_thread_messages(session, thread_id, run_id, raw_messages)

    # Fast-fail (see souk.health's queued-timeout sweep for the fallback
    # covering the race where the target goes offline *after* this check):
    # if souk already knows the target is offline right now, don't queue at
    # all — emit a single terminal event and close instead of opening a
    # stream that would otherwise sit idle until queued_timeout_seconds.
    if not repo.is_agent_online(agent["last_seen_at"]):
        await repo.mark_run_status(
            session, run_id, "failed", metadata={"failureReason": "agent_offline"}
        )
        await session.commit()

        async def offline_stream():
            # A real run always announces its own thread_id/run_id via
            # RUN_STARTED first (see pydantic_ai's AGUIAdapter — it
            # copies straight from the RunAgentInput it was given, which
            # is already the resolved, real id) — this fast-fail path
            # never reaches a provider to emit one, so it synthesizes
            # the same standard, in-band announcement itself rather than
            # relying on a custom header (see
            # souk-no-forced-protocol-deviation and RunErrorEvent's own
            # schema, which has no thread_id/run_id field to carry this
            # otherwise).
            yield {
                "event": "message",
                "data": json.dumps(
                    {"type": "RUN_STARTED", "threadId": thread_id, "runId": run_id}
                ),
            }
            yield {
                "event": "message",
                "data": json.dumps({"type": "RUN_ERROR", "message": "agent is currently offline"}),
            }

        return EventSourceResponse(offline_stream())

    try:
        input_json = build_run_agent_input(
            thread_id,
            run_id,
            messages,
            state=body.state,
            tools=[t.model_dump(mode="json", by_alias=True) for t in body.tools],
            context=[c.model_dump(mode="json", by_alias=True) for c in body.context],
            forwarded_props=_build_forwarded_props(run_id, agent_id, metadata, body.forwarded_props),
            resume=resume,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    await session.commit()

    run = broker.enqueue_run(run_id, agent_id, thread_id, input_json, "ag-ui", HANDLERS, seq=starting_seq)

    async def event_stream():
        # Maps the agent's own provider-generated messageId (e.g.
        # pydantic-ai's AGUIAdapter mints a plain uuid4) to a souk-assigned
        # one, consistently across a message's START/CONTENT/END events —
        # see souk.agui.rewrite_message_ids.
        message_id_map: dict[str, str] = {}
        # No `finally` here, deliberately: this loop exiting some other
        # way than drain_run's natural end (the caller disconnected, or
        # this generator got closed out from under it) does not cancel
        # the run. souk's own DB state is authoritative and the run keeps
        # going regardless of whether anyone's still watching this
        # particular stream — a dropped connection (network blip, tab
        # closed and reopened) shouldn't throw away in-progress work a
        # reconnect (GET /threads/{thread_id}) could otherwise catch up
        # on. Cancelling a run is an explicit act only (see A2A's
        # tasks/cancel; AG-UI has no equivalent endpoint yet). The run's
        # own pipeline task forgets it from the registry once it
        # naturally finishes either way — nothing for this generator to
        # do on its way out.
        async for item in drain_run(run):
            yield {"event": "message", "data": json.dumps(rewrite_message_ids(item, message_id_map))}

    # No X-Souk-Thread-Id/X-Souk-Run-Id headers — a real run's own first
    # event is RUN_STARTED, which every compliant AG-UI provider emits
    # with threadId/runId copied straight from the RunAgentInput it was
    # given (already the resolved, real ids) — that's the standard,
    # in-band place a client learns them, not a souk-invented header.
    return EventSourceResponse(event_stream())


@router.post("/agui/id/{agent_id}", response_model=None)
async def run_agent_by_id(
    agent_id: str, body: RunAgentInput, session: AsyncSession = Depends(get_session)
) -> EventSourceResponse | JSONResponse:
    return await _run_agent(agent_id, body, session)


@router.post("/agui/{name}", response_model=None)
async def run_agent_by_name(
    name: str, body: RunAgentInput, session: AsyncSession = Depends(get_session)
) -> EventSourceResponse | JSONResponse:
    agent_id = await _resolve_agent_id(session, name)
    return await _run_agent(agent_id, body, session)
