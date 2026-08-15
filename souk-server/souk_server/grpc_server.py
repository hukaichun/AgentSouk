"""gRPC servicer implementing proto/souk.proto's SoukAgentGateway.

This file is transport and nothing else. What a run *means* — persisting
events, deciding a final status, reducing a reply into thread history —
lives in souk/handlers.py, in core; this only carries bytes to and from it.

The remote worker loop is the same loop souk runs for an in-process one (see
souk/worker.py); these two RPCs are just how it reaches across a wire:

    PollForWork   -> Souk.claim_work    — claim runs, and receive their input
    AgentSession  -> Souk.report_event  — push each event back, per run
                     Souk.finish_run    — and say when the stream ended
                  <- a cancel frame     — souk asking a run to stop

AgentSession is one persistent, multiplexed stream per SDK client
connection: every run that client currently has in flight pushes through
this single connection, disambiguated by run_id on each envelope.

Nothing in here keeps per-run state. It used to: a `GrpcProvider` held a
queue per run so core could iterate one stream per run, which is what made an
event cross two routing tables before it reached the run's own pipeline. Core
already has the only table needed — the broker's run registry — so a frame
now goes straight there by run_id (see docs/library-architecture.md).

A finished run gets nothing back: the SDK's `end_of_stream` frame is the
last word on it (see souk.handlers._handle_finish for why there is no
completion acknowledgement).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from functools import partial
from typing import TYPE_CHECKING

import grpc

from souk_server.config import ServingSettings
from souk.errors import InvalidRegistration
from souk_server.grpc_gen import souk_pb2, souk_pb2_grpc
from souk.identity import verify_session_token

if TYPE_CHECKING:
    from souk.core import Souk

logger = logging.getLogger("souk.grpc")


def _bearer(context) -> str:
    """The session token off the call's metadata, empty if absent. Verifying
    it is core's job (Souk.claim_work); lifting it out of gRPC metadata is
    this file's."""
    for key, value in context.invocation_metadata() or ():
        if key == "authorization":
            return value
    return ""


def _authenticate(context, signing_secret: str) -> str | None:
    """Returns the public key this call is authenticated as, or None if the
    token is missing/invalid/expired. Both RPCs need the *identity*, not just
    a yes/no: it is what every claimed run is recorded against and what every
    reported event is checked against (see Souk.report_event). A provider has
    no other id — nothing it calls itself is anything souk verified.
    """
    token = _bearer(context)
    return verify_session_token(token, signing_secret) if token else None


class WorkerSessions:
    """Which connected providers souk can currently reach, by public key.

    Exists for exactly one message — "please stop run X" — because that is
    the only thing souk ever sends a worker, and a request has to reach a
    connection. Keyed by client rather than by run: a run's claim happens on
    PollForWork, possibly before this client has any stream open at all, so
    binding the ask to whichever stream that identity has *when souk asks*
    is the only thing that works. It is also why this is not a routing table
    in the sense the old GrpcProvider._runs was — nothing an agent produces
    passes through here.

    Keyed by identity rather than by connection, so this is also how a
    provider running two processes behaves correctly: both are the same
    provider, either may hold the run, and asking both is harmless — a stream
    that doesn't have the run ignores the frame. The same goes for one client
    briefly holding two streams across a reconnect.
    """

    def __init__(self) -> None:
        self._outbound: dict[str, set[asyncio.Queue]] = defaultdict(set)

    def add(self, public_key: str, outbound: asyncio.Queue) -> None:
        self._outbound[public_key].add(outbound)

    def remove(self, public_key: str, outbound: asyncio.Queue) -> None:
        self._outbound[public_key].discard(outbound)
        if not self._outbound[public_key]:
            del self._outbound[public_key]

    def notify_cancel(self, public_key: str, run_id: str) -> None:
        """Put souk's cancel request on the wire. Synchronous and
        best-effort by design: this returning says only that the request was
        queued for the worker, never that anything stopped. Whether the agent
        stops, and when, is the worker's business — souk finds out by what
        the run's stream does next (see souk.handlers._handle_cancel).
        """
        streams = self._outbound.get(public_key)
        if not streams:
            # Nothing open to ask on: the worker is between connections, or
            # gone. The run keeps running until it ends or the health sweep
            # gives up on it — souk records no outcome it hasn't observed.
            logger.info("cancel for run %s: no open session for %s", run_id, public_key)
            return
        for outbound in streams:
            outbound.put_nowait(souk_pb2.AgentEventEnvelope(run_id=run_id, cancel=True))


class SoukAgentGatewayServicer(souk_pb2_grpc.SoukAgentGatewayServicer):
    def __init__(self, souk: "Souk") -> None:
        self._souk = souk
        self._sessions = WorkerSessions()

    async def PollForWork(self, request, context):
        """Framing only. Who may claim what, filtering to owned agents, the
        long-poll wait and marking agents seen are all one domain act, and
        live on Souk.claim_work — so a second transport implements framing
        rather than re-deriving any of it (see souk/core.py).

        The response carries each run's RunAgentInput, because claiming is
        the hand-over: there is no follow-up frame in which souk delivers the
        input, and so no window where a worker holds a run it hasn't been
        told how to run.
        """
        public_key = _authenticate(context, self._souk.settings.token_signing_secret)
        if public_key is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid session token")

        try:
            runs = await self._souk.claim_work(
                _bearer(context),
                list(request.agent_ids),
                max_claim=request.max_claim if request.HasField("max_claim") else None,
                wait_seconds=request.wait_seconds if request.HasField("wait_seconds") else 0,
                on_cancel=partial(self._sessions.notify_cancel, public_key),
            )
        except InvalidRegistration as e:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(e))

        return souk_pb2.PollResponse(
            pending=[
                souk_pb2.PendingRun(
                    run_id=run.run_id,
                    agent_id=run.agent_id,
                    json_payload=json.dumps(run.run_input),
                )
                for run in runs
            ]
        )

    async def AgentSession(
        self, request_iterator: AsyncIterator[souk_pb2.AgentEventEnvelope], context
    ):
        public_key = _authenticate(context, self._souk.settings.token_signing_secret)
        if public_key is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid session token")

        souk = self._souk
        outbound: asyncio.Queue = asyncio.Queue()
        self._sessions.add(public_key, outbound)

        async def handle_incoming() -> None:
            # Unwrap and hand over, nothing else. Every frame names the run
            # it belongs to and core looks it up in the one table that
            # exists; this loop keeps no state of its own, so there is
            # nothing here for a bad frame to corrupt and no run whose
            # events depend on this connection still being the one that
            # claimed it. Whether this client may speak for that run_id at
            # all is core's check, not a matter of having connected.
            async for envelope in request_iterator:
                if envelope.end_of_stream:
                    souk.finish_run(envelope.run_id, claimed_by=public_key)
                elif envelope.json_payload:
                    souk.report_event(
                        envelope.run_id, json.loads(envelope.json_payload), claimed_by=public_key
                    )
                else:
                    # Nothing to relay. An SDK older than the worker model
                    # sends one of these per run to announce a claim; there
                    # is nothing to announce now (claiming carries the
                    # input), and one confused client must not take its own
                    # connection down with a parse error.
                    logger.warning(
                        "AgentSession: empty envelope for run_id=%s from %s — an SDK "
                        "predating the worker model?",
                        envelope.run_id,
                        public_key,
                    )

        reader = asyncio.create_task(handle_incoming())
        try:
            while True:
                item = await outbound.get()
                yield item
        finally:
            reader.cancel()
            self._sessions.remove(public_key, outbound)
            # Deliberately nothing else. This connection going away is not
            # evidence that the runs it was carrying have ended — the worker
            # may reconnect and report them (a run is addressed by id, not
            # by connection). souk used to synthesise a stream-ending for
            # every one of them here, which is precisely the kind of outcome
            # it never observed. A worker that really is gone is caught by
            # the stall sweep instead (see souk/health.py).


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
