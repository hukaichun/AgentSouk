import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from souk import repo
from souk.agui import build_run_agent_input, fill_message_ids, rewrite_message_ids
from souk.broker import broker, drain_run
from souk.grpc_server import HANDLERS
from souk.db import get_session
from souk.kyok import issue_kyok_token
from souk.models import RunAgentInput

router = APIRouter()


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
    appears if the caller set metadata.parentThreadId when it called
    through souk (see api_a2a._start_run) — souk can't discover this on its
    own for calls that didn't opt in.
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


def _build_forwarded_props(run_id: str, agent_id: str, metadata: dict) -> dict | None:
    """Keep Your Own Key opt-in: a caller that's running its own KYOK
    bridge (see souk.api_llm_bridge / souk-client-sdk's KyokBridge) passes
    `metadata.kyok.sessionId` — the session it's already long-polling
    `/kyok/poll` for — when starting the run. If present, mint a
    run-scoped token binding this run_id (and the agent_id it's assigned
    to — souk already knows this at mint time, so the token says so
    explicitly rather than leaving it implicit; see api_llm_bridge.
    chat_completions for where that's checked) to that session, and hand
    it to the provider via AG-UI's forwardedProps, the existing
    passthrough field for exactly this kind of app-specific context (see
    souk.agui.build_run_agent_input). A provider that doesn't look for
    forwardedProps.kyok just never sees it and calls its own configured
    LLM as always — KYOK is opt-in on both the caller's and the
    provider's side independently, souk never forces either.

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
        return None
    return {"kyok": {"token": issue_kyok_token(run_id, session_id, agent_id)}}


async def _run_agent(
    agent_id: str, body: RunAgentInput, session: AsyncSession
) -> EventSourceResponse | JSONResponse:
    agent = await repo.get_agent_by_id(session, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent '{agent_id}' is not registered")

    if body.thread_id is not None:
        thread = await repo.get_thread(session, body.thread_id)
        if thread is None:
            raise HTTPException(status_code=404, detail=f"thread '{body.thread_id}' not found")
        if thread["agent_id"] != agent_id:
            raise HTTPException(
                status_code=409,
                detail=f"thread '{body.thread_id}' belongs to agent '{thread['agent_id']}', not '{agent_id}'",
            )
        # A thread only ever has one active run at a time (see
        # repo.get_active_run_for_thread) — starting a second one
        # concurrently would fork its otherwise-linear history with no
        # clean way to merge it back. Instead of erroring or silently
        # queueing a duplicate, hand back the thread's current state
        # (including the pending run's real status) so the caller can act
        # on it — this doubles as the "catch me up" path for a run that
        # has since paused.
        active = await repo.get_active_run_for_thread(session, body.thread_id)
        if active is not None and not (body.resume and active["status"] == "input-required"):
            snapshot = await repo.get_thread_snapshot(session, body.thread_id)
            return JSONResponse(
                jsonable_encoder(snapshot),
                headers={"X-Souk-Thread-Id": body.thread_id, "X-Souk-Run-Id": active["run_id"]},
            )
        resuming_run_id = active["run_id"] if active is not None else None
    else:
        resuming_run_id = None

    thread_id = await repo.ensure_thread(session, agent_id, body.thread_id, metadata=body.metadata)

    if resuming_run_id is not None:
        # Reopens the *same* run_id for another round rather than
        # minting a new one — see repo.reopen_run's docstring for why a
        # stable identity across pause/resume rounds matters (it's what
        # lets a delegating caller's waitingOnRunId subscription, and
        # this run's own task_id if it's an A2A one, stay valid without
        # ever needing to be retargeted or chased through a chain).
        run_id = resuming_run_id
        starting_seq = await repo.get_last_event_seq(session, run_id)
        await repo.reopen_run(session, run_id, body.model_dump(mode="json"), metadata=body.metadata)
    else:
        created = await repo.create_run(
            session, thread_id, agent_id, "ag-ui", body.model_dump(mode="json"), metadata=body.metadata
        )
        run_id = created["run_id"]
        starting_seq = 0

    messages = fill_message_ids(body.messages)
    await repo.append_thread_messages(session, thread_id, run_id, messages)

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
            yield {
                "event": "message",
                "data": json.dumps({"type": "RUN_ERROR", "message": "agent is currently offline"}),
            }

        return EventSourceResponse(
            offline_stream(),
            headers={"X-Souk-Thread-Id": thread_id, "X-Souk-Run-Id": run_id},
        )

    try:
        input_json = build_run_agent_input(
            thread_id,
            run_id,
            messages,
            forwarded_props=_build_forwarded_props(run_id, agent_id, body.metadata),
            resume=body.resume,
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

    return EventSourceResponse(
        event_stream(),
        headers={"X-Souk-Thread-Id": thread_id, "X-Souk-Run-Id": run_id},
    )


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
