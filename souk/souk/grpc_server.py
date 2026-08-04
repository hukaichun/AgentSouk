"""gRPC servicer implementing proto/souk.proto's SoukAgentGateway, plus
the handler functions ("function objects" — see broker.py's module
docstring for the pipeline model these plug into) that actually do
something when a Command is applied to a Run: HANDLERS at the bottom of
this file is the dispatch table every run's pipeline task uses.

AgentSession is one persistent, multiplexed stream per SDK client
connection — every run that client's agents pick up (via PollForWork) is
claimed, delivered, and drained through this single connection, not by
opening a new stream per run (see proto/souk.proto for the exact framing).
souk demultiplexes inbound envelopes by run_id and sends an explicit
`ack=true` envelope once it has fully persisted and relayed every event
for a run_id, so the SDK has confirmation the call was fully consumed —
not just sent — before it moves on.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import grpc

from souk import repo
from souk.agui import build_run_agent_input
from souk.broker import (
    END_OF_STREAM,
    Claim,
    Fail,
    FinishStream,
    HandlerMap,
    RelayEvent,
    RequestCancel,
    Run,
    broker,
)
from souk.config import settings
from souk.db import SessionLocal
from souk.grpc_gen import souk_pb2, souk_pb2_grpc
from souk.identity import verify_session_token
from souk.pause import is_pause_event

logger = logging.getLogger("souk.grpc")


def _authenticate(context) -> str | None:
    """Every gRPC call must present the bearer token issued at
    /agents/register (see souk/identity.py) — returns its sdk_client_id,
    or None if missing/invalid/expired. Defense in depth: PollForWork
    additionally filters requested agent_ids down to ones this
    sdk_client_id actually owns (see repo.get_agent_ids_for_sdk_client),
    since a token alone doesn't say *which* names its holder controls.
    """
    for key, value in context.invocation_metadata() or ():
        if key == "authorization":
            return verify_session_token(value)
    return None


def _last_assistant_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "assistant":
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


# ---- Handlers: each owns exactly one Command type, and is the only code
# (besides broker._pipeline's dispatch loop itself) that ever touches a
# Run's fields — see broker.py's module docstring for why that's the
# whole point.


async def _handle_claim(run: Run, cmd: Claim) -> None:
    run.agent_outbound = cmd.outbound
    # If already cancelled here, RequestCancel's own DB write is still
    # queued *behind* this Claim (see Run.cancelled's docstring — the
    # flag is set synchronously, ahead of the queue, so this can observe
    # it true before _handle_cancel has actually run) — don't race it by
    # also writing "running" here; let that queued command own the DB
    # write, as it would for any other cancel.
    if not run.cancelled:
        async with SessionLocal() as session:
            await repo.mark_run_status(session, run.run_id, "running")
    # Always deliver the real input regardless: the agent's _handle_run
    # parses whatever comes back from its claim as RunAgentInput JSON
    # unconditionally (see souk_agent_sdk.client) — an empty/cancel-only
    # envelope here would just make it crash trying to json.loads(""),
    # not cancel it cleanly.
    await cmd.outbound.put(
        souk_pb2.AgentEventEnvelope(
            run_id=run.run_id,
            agent_id=run.agent_id,
            json_payload=json.dumps(run.input_json),
        )
    )
    if run.cancelled:
        # broker.poll() already filters out cancelled runs before
        # they're ever handed to an agent — this only fires in the
        # narrow window where a run was cancelled *after* poll() handed
        # it out but *before* this claim envelope arrived. Following up
        # immediately with a cancel envelope reuses the agent's normal
        # consume()-vs-watch_cancel() race (see souk_agent_sdk.client's
        # _handle_run) instead of inventing a new envelope shape for it
        # — the queued RequestCancel behind this Claim will still run
        # next and do its own (redundant but harmless) send/DB-write.
        await cmd.outbound.put(
            souk_pb2.AgentEventEnvelope(run_id=run.run_id, agent_id=run.agent_id, cancel=True)
        )


async def _handle_relay(run: Run, cmd: RelayEvent) -> None:
    # Persist *before* relaying: if souk crashes between the two, the
    # live caller must not end up having seen an event that was never
    # durably recorded.
    event = json.loads(cmd.json_payload)
    if is_pause_event(event):
        # Remembered for _handle_finish, which decides the run's final
        # status once the stream actually ends — a pause event isn't
        # itself a stream terminator (see souk/pause.py).
        run.pause_payload = event.get("value") or {}
    run.seq += 1
    async with SessionLocal() as session:
        await repo.append_run_event(session, run.run_id, run.seq, event)
        # Marks the run as still making progress — see
        # repo.fail_stalled_runs, which would otherwise eventually treat
        # a quiet-but-alive run as abandoned.
        await repo.touch_run_activity(session, run.run_id)
        await session.commit()
    await run.out_queue.put(event)


async def _handle_finish(run: Run, cmd: FinishStream) -> None:
    # A run already cancelled (see _handle_cancel) reaching FinishStream
    # is the agent unwinding after being told to stop, not a real
    # completion — the DB status must stay "cancelled", not get
    # overwritten.
    if not run.cancelled:
        async with SessionLocal() as session:
            if run.pause_payload is not None:
                await repo.mark_run_status(session, run.run_id, "input-required", metadata=run.pause_payload)
            else:
                await repo.mark_run_status(session, run.run_id, "completed")
    await run.out_queue.put(END_OF_STREAM)
    if run.agent_outbound is not None:
        await run.agent_outbound.put(
            souk_pb2.AgentEventEnvelope(run_id=run.run_id, agent_id=run.agent_id, ack=True)
        )
    # A plain completion (not a pause, not a cancellation) may be exactly
    # what an outer run was waiting on — see souk/pause.py's
    # waitingOnRunId. Best-effort: this only ever matters for
    # sub-agent-style delegation, so a failure here is logged, not
    # allowed to break the run that just legitimately finished.
    if not run.cancelled and run.pause_payload is None:
        try:
            await _resume_parent_run_if_waiting(run.run_id, run.thread_id, "completed")
        except Exception:
            logger.exception("failed to check/resume run waiting on run=%s", run.run_id)


async def _handle_cancel(run: Run, cmd: RequestCancel) -> None:
    """The async half of a cancellation — see broker.request_cancel,
    which is what actually queues this and has already set
    `run.cancelled = True` synchronously before this ever runs (do not
    set it here too; some callers, e.g. _handle_claim's own narrow-race
    check, depend on observing it true *before* this handler has had a
    chance to run at all — see Run.cancelled's docstring). This handler
    is just the DB write and telling the agent side (if it already
    claimed this run) to stop producing further events, so it doesn't
    linger as 'running' until repo.fail_stalled_runs eventually sweeps
    it.

    If broker.poll() never handed this run to any agent at all,
    `agent_outbound` stays None forever and there's no connection to
    signal — broker._pipeline treats that combination as terminal and
    forgets the run right after this returns.
    """
    async with SessionLocal() as session:
        await repo.mark_run_status(session, run.run_id, "cancelled")
    if run.agent_outbound is not None:
        await run.agent_outbound.put(
            souk_pb2.AgentEventEnvelope(run_id=run.run_id, agent_id=run.agent_id, cancel=True)
        )
    await run.out_queue.put(END_OF_STREAM)
    # Mirrors _handle_finish's auto-resume: a run waiting on this one
    # (see souk/pause.py's waitingOnRunId) must not be left stranded
    # just because *this* delegated call was cancelled instead of
    # completing normally — see _resume_parent_run_if_waiting's status
    # handling.
    try:
        await _resume_parent_run_if_waiting(run.run_id, run.thread_id, "cancelled")
    except Exception:
        logger.exception("failed to check/resume run waiting on run=%s", run.run_id)


async def _handle_fail(run: Run, cmd: Fail) -> None:
    """The health sweep gave up on this run — see souk/health.py."""
    event = {"type": "RUN_ERROR", "message": cmd.reason}
    run.seq += 1
    async with SessionLocal() as session:
        await repo.append_run_event(session, run.run_id, run.seq, event)
        await repo.mark_run_status(session, run.run_id, "failed", metadata={"failureReason": cmd.reason})
    await run.out_queue.put(event)
    await run.out_queue.put(END_OF_STREAM)
    # Same reasoning as _handle_cancel above: a failed delegated call must
    # still wake up whatever's waiting on it, not strand it.
    try:
        await _resume_parent_run_if_waiting(run.run_id, run.thread_id, "failed", cmd.reason)
    except Exception:
        logger.exception("failed to check/resume run waiting on run=%s", run.run_id)


async def _resume_parent_run_if_waiting(
    child_run_id: str, child_thread_id: str, status: str, reason: str | None = None
) -> None:
    """`status` is the delegated child run's own terminal outcome
    ('completed' / 'cancelled' / 'failed' — see the three call sites in
    _handle_finish/_handle_cancel/_handle_fail above) — forwarded to the
    waiting run so its own agent (and, through it, its own LLM turn) can
    react differently to a sub-agent that actually finished versus one
    that never did, rather than always being told a `result` that
    silently means "nothing came back."

    Looked up by `child_run_id` specifically, not `child_thread_id` —
    see repo.find_run_waiting_on's docstring for why thread-level
    matching was ambiguous. `child_thread_id` is only needed here to
    fetch this thread's message history for the 'completed' case below.
    """
    async with SessionLocal() as session:
        parent_run = await repo.find_run_waiting_on(session, child_run_id)
        if parent_run is None:
            return
        parent_thread_id = parent_run["thread_id"]
        if status == "completed":
            child_messages = await repo.get_thread_messages(session, child_thread_id)
            result_text = _last_assistant_text(child_messages)
        else:
            result_text = reason or status
        forwarded_props = {
            "resume": {
                "waitingOnThreadId": child_thread_id,
                "status": status,
                "result": result_text,
            }
        }
        resume_input = {"resume": forwarded_props["resume"]}
        if parent_run["status"] == "input-required":
            # Reopens the *same* run_id/task_id for another round — see
            # repo.reopen_run's docstring for why a stable identity
            # across pause/resume rounds is what makes waitingOnRunId
            # (and a caller's task_id) not need any retargeting.
            new_run_id = parent_run["run_id"]
            starting_seq = await repo.get_last_event_seq(session, new_run_id)
            await repo.reopen_run(
                session, new_run_id, resume_input, metadata={"resumedFrom": child_thread_id}
            )
        else:
            # Already 'completed' (see repo.find_run_waiting_on's
            # docstring) — it produced a real answer of its own and was
            # never paused, so there's no run to reopen: a genuinely new
            # run/turn is needed to react to this. Clear this specific
            # subscription first so the old row can't match again
            # (defense in depth — under normal operation child_run_id's
            # own terminal transition only ever fires once, so this is
            # belt-and-suspenders, not load-bearing). Must not touch its
            # status/timestamps (see merge_run_metadata's docstring).
            await repo.merge_run_metadata(session, parent_run["run_id"], {"waitingOnRunId": None})
            created = await repo.create_run(
                session,
                parent_thread_id,
                parent_run["agent_id"],
                parent_run["protocol"],
                resume_input,
                metadata={"resumedFrom": child_thread_id},
            )
            new_run_id = created["run_id"]
            starting_seq = 0
        parent_messages = await repo.get_thread_messages(session, parent_thread_id)
        try:
            input_json = build_run_agent_input(
                parent_thread_id, new_run_id, parent_messages, forwarded_props=forwarded_props
            )
        except ValueError:
            logger.exception("failed to build resume RunAgentInput for thread=%s", parent_thread_id)
            await repo.mark_run_status(session, new_run_id, "failed", metadata={"failureReason": "resume_build_failed"})
            return
        await session.commit()
    broker.enqueue_run(
        new_run_id,
        parent_run["agent_id"],
        parent_thread_id,
        input_json,
        parent_run["protocol"],
        HANDLERS,
        seq=starting_seq,
    )
    logger.info(
        "auto-resumed run=%s on thread=%s after child thread=%s completed",
        new_run_id,
        parent_thread_id,
        child_thread_id,
    )


HANDLERS: HandlerMap = {
    Claim: _handle_claim,
    RelayEvent: _handle_relay,
    FinishStream: _handle_finish,
    RequestCancel: _handle_cancel,
    Fail: _handle_fail,
}


class SoukAgentGatewayServicer(souk_pb2_grpc.SoukAgentGatewayServicer):
    async def PollForWork(self, request, context):
        sdk_client_id = _authenticate(context)
        if sdk_client_id is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid session token")

        requested_ids = list(request.agent_ids)
        async with SessionLocal() as session:
            owned_ids = await repo.get_agent_ids_for_sdk_client(session, sdk_client_id)
        agent_ids = [agent_id for agent_id in requested_ids if agent_id in owned_ids]
        if len(agent_ids) != len(requested_ids):
            logger.warning(
                "PollForWork: sdk_client_id=%s requested unowned agent id(s): %s",
                sdk_client_id,
                sorted(set(requested_ids) - owned_ids),
            )

        max_claim = request.max_claim if request.HasField("max_claim") else None
        runs = broker.poll(agent_ids, max_claim=max_claim)

        wait_seconds = request.wait_seconds if request.HasField("wait_seconds") else 0
        # No point holding the call open when the caller explicitly
        # reported zero spare capacity (max_claim=0) — nothing souk-side
        # will change that; capacity only frees up on the caller's end.
        if not runs and wait_seconds > 0 and max_claim != 0:
            event = broker.subscribe_wake(agent_ids)
            try:
                await asyncio.wait_for(event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
            finally:
                broker.unsubscribe_wake(agent_ids, event)
            runs = broker.poll(agent_ids, max_claim=max_claim)

        async with SessionLocal() as session:
            for agent_id in agent_ids:
                await repo.touch_agent(session, agent_id)

        return souk_pb2.PollResponse(
            pending=[
                souk_pb2.PendingRun(run_id=r.run_id, agent_id=r.agent_id) for r in runs
            ]
        )

    async def AgentSession(
        self, request_iterator: AsyncIterator[souk_pb2.AgentEventEnvelope], context
    ):
        if _authenticate(context) is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid session token")

        outbound: asyncio.Queue = asyncio.Queue()

        async def handle_incoming() -> None:
            # Just routes each envelope into the target run's own
            # in_queue as a Command — no actual work happens here, so
            # nothing in this loop can raise from DB/business logic
            # anymore (that all moved into the run's own pipeline task —
            # see broker._pipeline), and one run's bad command can no
            # longer take out this whole connection's dispatch loop.
            async for envelope in request_iterator:
                run_id = envelope.run_id
                run = broker.get(run_id)
                if run is None:
                    logger.warning("AgentSession: frame for unknown/finished run_id=%s", run_id)
                    continue
                if envelope.end_of_stream:
                    run.in_queue.put_nowait(FinishStream())
                elif not envelope.json_payload:
                    run.in_queue.put_nowait(Claim(outbound))
                else:
                    run.in_queue.put_nowait(RelayEvent(envelope.json_payload))

        reader = asyncio.create_task(handle_incoming())
        try:
            while True:
                item = await outbound.get()
                yield item
        finally:
            reader.cancel()


def create_grpc_server() -> grpc.aio.Server:
    server = grpc.aio.server()
    souk_pb2_grpc.add_SoukAgentGatewayServicer_to_server(SoukAgentGatewayServicer(), server)
    address = f"{settings.grpc_host}:{settings.grpc_port}"
    if settings.grpc_tls_cert_path and settings.grpc_tls_key_path:
        cert = open(settings.grpc_tls_cert_path, "rb").read()
        key = open(settings.grpc_tls_key_path, "rb").read()
        credentials = grpc.ssl_server_credentials([(key, cert)])
        server.add_secure_port(address, credentials)
        logger.info("gRPC server listening on %s with TLS", address)
    else:
        server.add_insecure_port(address)
        logger.warning(
            "gRPC server listening on %s WITHOUT TLS — fine for same-host development, "
            "never for a souk reachable over a real network (see souk.config's grpc_tls_* settings)",
            address,
        )
    return server
