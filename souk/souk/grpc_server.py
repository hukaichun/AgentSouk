"""gRPC servicer implementing proto/souk.proto's SoukAgentGateway.

This file is transport and nothing else. What a run *means* — persisting
events, deciding a final status, reducing a reply into thread history —
lives in souk/handlers.py, in core; this only carries bytes to and from it.

AgentSession is one persistent, multiplexed stream per SDK client
connection — every run that client's agents pick up (via PollForWork) is
claimed, delivered, and drained through this single connection, not by
opening a new stream per run (see proto/souk.proto for the exact framing).
GrpcProvider is what turns that one multiplexed stream back into the
one-stream-per-run shape souk's AgentProvider port asks for (see
souk/providers.py): the connection is held by the provider, and each run
gets its own queue fed by this file's demultiplexing loop. That is also why
"one run per call" in the port says nothing about connection count.

A finished run gets nothing back: the SDK's `end_of_stream` frame is the
last word on it (see souk.handlers._handle_finish for why there is no
completion acknowledgement).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import grpc

from souk.config import ServingSettings
from souk.errors import InvalidRegistration
from souk.grpc_gen import souk_pb2, souk_pb2_grpc
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
    """AgentSession's own check: returns the sdk_client_id, or None if the
    token is missing/invalid/expired. Defense in depth — PollForWork
    additionally filters requested agent_ids down to ones this holder really
    owns, since a token alone doesn't say *which* agents it controls."""
    token = _bearer(context)
    return verify_session_token(token, signing_secret) if token else None


class GrpcProvider:
    """One connected SDK client, presented as an AgentProvider.

    Satisfies souk/providers.py's port: `start(run_input)` delivers the input
    and hands back the event stream for that one run. The connection is held here, not per run, so every
    run this client claims is multiplexed over the same AgentSession stream —
    which is why the port being "one call per run" says nothing about how
    many connections a transport opens.

    `start` puts the input on the wire before it returns, which is exactly
    what the port promises: the SDK's run task blocks waiting for that frame,
    so a cancel arriving first would otherwise strand it.
    """

    # Sentinel queued when the agent sends end_of_stream for a run — ends
    # that run's iterator without ending the connection.
    _DONE = object()

    def __init__(self, outbound: asyncio.Queue) -> None:
        self.outbound = outbound
        self._runs: dict[str, asyncio.Queue] = {}
        self._agent_ids: dict[str, str] = {}

    async def start(self, agent_id: str, run_input: dict) -> AsyncIterator[Any]:
        run_id = run_input["runId"]
        # Kept only so this run's cancel frame can carry it: envelopes have an
        # agent_id field, and cancel arrives with just a run_id.
        self._agent_ids[run_id] = agent_id
        queue: asyncio.Queue = asyncio.Queue()
        self._runs[run_id] = queue
        await self.outbound.put(
            souk_pb2.AgentEventEnvelope(
                run_id=run_id, agent_id=agent_id, json_payload=json.dumps(run_input)
            )
        )
        return self._events(run_id, agent_id, queue)

    async def _events(self, run_id: str, agent_id: str, queue: asyncio.Queue) -> AsyncIterator[Any]:
        """Ends when the agent's own end_of_stream arrives, and not before —
        including for a cancelled run, which keeps being read until the agent
        actually stops (see souk.handlers._pump)."""
        try:
            while (item := await queue.get()) is not self._DONE:
                yield item
        finally:
            self._runs.pop(run_id, None)
            self._agent_ids.pop(run_id, None)

    async def cancel(self, run_id: str) -> None:
        """Put souk's cancel request on the wire. The SDK races it against
        the run in flight (see souk_agent_sdk.client's watch_cancel); whether
        the agent stops is its own business, and this returning says only
        that the request was sent."""
        await self.outbound.put(
            souk_pb2.AgentEventEnvelope(
                run_id=run_id, agent_id=self._agent_ids.get(run_id, ""), cancel=True
            )
        )

    def deliver_event(self, run_id: str, event: Any) -> None:
        queue = self._runs.get(run_id)
        if queue is None:
            # Straggler from a run souk already stopped reading — expected
            # after a cancel, not an error.
            return
        queue.put_nowait(event)

    def close_run(self, run_id: str) -> None:
        queue = self._runs.get(run_id)
        if queue is not None:
            queue.put_nowait(self._DONE)

    def close_all(self) -> None:
        """The connection is going away — end every run still reading from
        it rather than leaving their pumps blocked forever."""
        for queue in list(self._runs.values()):
            queue.put_nowait(self._DONE)
        self._runs.clear()
        self._agent_ids.clear()


class SoukAgentGatewayServicer(souk_pb2_grpc.SoukAgentGatewayServicer):
    def __init__(self, souk: "Souk") -> None:
        self._souk = souk

    async def PollForWork(self, request, context):
        """Framing only. Who may claim what, filtering to owned agents, the
        long-poll wait and marking agents seen are all one domain act, and
        live on Souk.claim_work — so a second transport implements framing
        rather than re-deriving any of it (see souk/core.py)."""
        try:
            runs = await self._souk.claim_work(
                _bearer(context),
                list(request.agent_ids),
                max_claim=request.max_claim if request.HasField("max_claim") else None,
                wait_seconds=request.wait_seconds if request.HasField("wait_seconds") else 0,
            )
        except InvalidRegistration as e:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, str(e))

        return souk_pb2.PollResponse(
            pending=[souk_pb2.PendingRun(run_id=r.run_id, agent_id=r.agent_id) for r in runs]
        )

    async def AgentSession(
        self, request_iterator: AsyncIterator[souk_pb2.AgentEventEnvelope], context
    ):
        if _authenticate(context, self._souk.settings.token_signing_secret) is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid session token")

        souk = self._souk
        provider = GrpcProvider(asyncio.Queue())

        async def handle_incoming() -> None:
            # Pure demultiplexing: every frame is routed either to the run's
            # own pipeline (a claim) or to that run's event queue inside the
            # provider. No DB or business logic happens here, so nothing in
            # this loop can raise from it, and one bad run can't take down
            # the whole connection's dispatch.
            async for envelope in request_iterator:
                run_id = envelope.run_id
                if not envelope.json_payload and not envelope.end_of_stream:
                    # A claim. Recording who took the run is core's call; all
                    # this file knows is that a claim frame arrived.
                    if not souk.assign_provider(run_id, provider):
                        logger.warning("AgentSession: claim for unknown/finished run_id=%s", run_id)
                elif envelope.end_of_stream:
                    provider.close_run(run_id)
                else:
                    provider.deliver_event(run_id, json.loads(envelope.json_payload))

        reader = asyncio.create_task(handle_incoming())
        try:
            while True:
                item = await provider.outbound.get()
                yield item
        finally:
            reader.cancel()
            provider.close_all()


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
