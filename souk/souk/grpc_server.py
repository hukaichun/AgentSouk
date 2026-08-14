"""gRPC servicer implementing proto/souk.proto's SoukAgentGateway, plus
the handler functions ("function objects" — see broker.py's module
docstring for the pipeline model these plug into) that actually do
something when a Command is applied to a Run: HANDLERS at the bottom of
this file is the dispatch table every run's pipeline task uses.

AgentSession is one persistent, multiplexed stream per SDK client
connection — every run that client's agents pick up (via PollForWork) is
claimed, delivered, and drained through this single connection, not by
opening a new stream per run (see proto/souk.proto for the exact framing).
souk demultiplexes inbound envelopes by run_id. A finished run gets nothing
back: the SDK's `end_of_stream` frame is the last word on it (see
_handle_finish for why there is no completion acknowledgement).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from functools import partial
from typing import TYPE_CHECKING

import grpc

from souk import repo
from souk.agui_reduce import reduce_events_to_messages
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
from souk.config import ServingSettings
from souk.grpc_gen import souk_pb2, souk_pb2_grpc
from souk.identity import verify_session_token
from souk.pause import interrupt_outcome_of

if TYPE_CHECKING:
    from souk.core import Souk

logger = logging.getLogger("souk.grpc")


def _authenticate(context, signing_secret: str) -> str | None:
    """Every gRPC call must present the bearer token issued at
    /agents/register (see souk/identity.py) — returns its sdk_client_id,
    or None if missing/invalid/expired. Defense in depth: PollForWork
    additionally filters requested agent_ids down to ones this
    sdk_client_id actually owns (see repo.get_agent_ids_for_sdk_client),
    since a token alone doesn't say *which* names its holder controls.
    """
    for key, value in context.invocation_metadata() or ():
        if key == "authorization":
            return verify_session_token(value, signing_secret)
    return None


# ---- Handlers: each owns exactly one Command type, and is the only code
# (besides broker._pipeline's dispatch loop itself) that ever touches a
# Run's fields — see broker.py's module docstring for why that's the
# whole point.


async def _handle_claim(souk: "Souk", run: Run, cmd: Claim) -> None:
    run.agent_outbound = cmd.outbound
    # If already cancelled here, RequestCancel's own DB write is still
    # queued *behind* this Claim (see Run.cancelled's docstring — the
    # flag is set synchronously, ahead of the queue, so this can observe
    # it true before _handle_cancel has actually run) — don't race it by
    # also writing "running" here; let that queued command own the DB
    # write, as it would for any other cancel.
    if not run.cancelled:
        async with souk.session() as session:
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


async def _handle_relay(souk: "Souk", run: Run, cmd: RelayEvent) -> None:
    # Persist *before* relaying: if souk crashes between the two, the
    # live caller must not end up having seen an event that was never
    # durably recorded.
    event = json.loads(cmd.json_payload)
    interrupts = interrupt_outcome_of(event)
    if interrupts is not None:
        # Remembered for _handle_finish, which decides the run's final
        # status once the stream actually ends — an interrupt outcome
        # isn't itself a stream terminator (see souk/pause.py).
        run.pause_payload = {"interrupts": interrupts}
    run.seq += 1
    async with souk.session() as session:
        await repo.append_run_event(session, run.run_id, run.seq, event)
        # Marks the run as still making progress — see
        # repo.fail_stalled_runs, which would otherwise eventually treat
        # a quiet-but-alive run as abandoned.
        await repo.touch_run_activity(session, run.run_id)
        await session.commit()
    await run.out_queue.put(event)


async def _handle_finish(souk: "Souk", run: Run, cmd: FinishStream) -> None:
    # A run already cancelled (see _handle_cancel) reaching FinishStream
    # is the agent unwinding after being told to stop, not a real
    # completion — the DB status must stay "cancelled", not get
    # overwritten.
    if not run.cancelled:
        async with souk.session() as session:
            if run.pause_payload is not None:
                await repo.mark_run_status(session, run.run_id, "input-required", metadata=run.pause_payload)
            else:
                await repo.mark_run_status(session, run.run_id, "completed")
            # A genuine reply was produced (as opposed to failed/
            # cancelled — see souk/agui_reduce.py's module docstring for
            # why those don't go through this at all) — persist it as
            # real thread_history messages so souk is an actual source
            # of truth for the full conversation, not just the caller's
            # half of it. Only this round's own events (see
            # Run.round_starting_seq) — a resumed run's earlier round(s)
            # were already persisted the first time they finished.
            round_events = await repo.get_run_events(session, run.run_id, since_seq=run.round_starting_seq)
            reply_messages = reduce_events_to_messages(round_events)
            if reply_messages:
                await repo.append_thread_messages(session, run.thread_id, run.run_id, reply_messages)
            await session.commit()
    # Nothing goes back to the agent here. souk used to send an `ack=true`
    # envelope at this point, once everything was persisted; it was removed
    # because the agent could only ever log it — it has already produced and
    # discarded its events, so there is no recovery action available to it if
    # souk failed to persist. Whether a run is durable is a question its
    # *caller* asks, via the run's own status. See proto/souk.proto's
    # reserved field 5.
    await run.out_queue.put(END_OF_STREAM)


async def _handle_cancel(souk: "Souk", run: Run, cmd: RequestCancel) -> None:
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
    async with souk.session() as session:
        await repo.mark_run_status(session, run.run_id, "cancelled")
    if run.agent_outbound is not None:
        await run.agent_outbound.put(
            souk_pb2.AgentEventEnvelope(run_id=run.run_id, agent_id=run.agent_id, cancel=True)
        )
    await run.out_queue.put(END_OF_STREAM)


async def _handle_fail(souk: "Souk", run: Run, cmd: Fail) -> None:
    """The health sweep gave up on this run — see souk/health.py."""
    event = {"type": "RUN_ERROR", "message": cmd.reason}
    run.seq += 1
    async with souk.session() as session:
        await repo.append_run_event(session, run.run_id, run.seq, event)
        await repo.mark_run_status(session, run.run_id, "failed", metadata={"failureReason": cmd.reason})
    await run.out_queue.put(event)
    await run.out_queue.put(END_OF_STREAM)


def make_handlers(souk: "Souk") -> HandlerMap:
    """The dispatch table every run's pipeline task uses, bound to one
    souk instance. A factory rather than a module-level dict because the
    handlers need a database to write to, and that now comes from an
    explicitly-constructed Souk rather than an import-time global — see
    souk/core.py. Cheap enough to call per run (five partials); the whole
    table moves into core in a later step (docs/library-architecture.md).
    """
    return {
        Claim: partial(_handle_claim, souk),
        RelayEvent: partial(_handle_relay, souk),
        FinishStream: partial(_handle_finish, souk),
        RequestCancel: partial(_handle_cancel, souk),
        Fail: partial(_handle_fail, souk),
    }


class SoukAgentGatewayServicer(souk_pb2_grpc.SoukAgentGatewayServicer):
    def __init__(self, souk: "Souk") -> None:
        self._souk = souk

    async def PollForWork(self, request, context):
        souk = self._souk
        sdk_client_id = _authenticate(context, souk.settings.token_signing_secret)
        if sdk_client_id is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid session token")

        requested_ids = list(request.agent_ids)
        async with souk.session() as session:
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

        async with souk.session() as session:
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
        if _authenticate(context, self._souk.settings.token_signing_secret) is None:
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


def create_grpc_server(souk: "Souk", settings: ServingSettings) -> grpc.aio.Server:
    server = grpc.aio.server()
    souk_pb2_grpc.add_SoukAgentGatewayServicer_to_server(SoukAgentGatewayServicer(souk), server)
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
