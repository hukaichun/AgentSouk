"""gRPC servicer implementing proto/souk.proto's SoukAgentGateway.

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
from souk.broker import broker
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
    additionally filters requested agent_names down to ones this
    sdk_client_id actually owns (see repo.get_agent_names_for_sdk_client),
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


class SoukAgentGatewayServicer(souk_pb2_grpc.SoukAgentGatewayServicer):
    async def PollForWork(self, request, context):
        sdk_client_id = _authenticate(context)
        if sdk_client_id is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid session token")

        requested_names = list(request.agent_names)
        async with SessionLocal() as session:
            owned_names = await repo.get_agent_names_for_sdk_client(session, sdk_client_id)
        agent_names = [name for name in requested_names if name in owned_names]
        if len(agent_names) != len(requested_names):
            logger.warning(
                "PollForWork: sdk_client_id=%s requested unowned agent name(s): %s",
                sdk_client_id,
                sorted(set(requested_names) - owned_names),
            )

        max_claim = request.max_claim if request.HasField("max_claim") else None
        states = broker.poll(agent_names, max_claim=max_claim)

        async with SessionLocal() as session:
            for name in agent_names:
                await repo.touch_agent(session, name)

        return souk_pb2.PollResponse(
            pending=[
                souk_pb2.PendingRun(run_id=s.run_id, agent_name=s.agent_name) for s in states
            ]
        )

    async def AgentSession(
        self, request_iterator: AsyncIterator[souk_pb2.AgentEventEnvelope], context
    ):
        if _authenticate(context) is None:
            await context.abort(grpc.StatusCode.UNAUTHENTICATED, "missing or invalid session token")

        outbound: asyncio.Queue = asyncio.Queue()

        async def handle_incoming() -> None:
            async for envelope in request_iterator:
                run_id = envelope.run_id
                try:
                    if envelope.end_of_stream:
                        await self._finish_run(run_id, outbound)
                    elif not envelope.json_payload:
                        await self._claim_run(run_id, outbound)
                    else:
                        await self._relay_event(run_id, envelope.json_payload)
                except Exception:
                    logger.exception("AgentSession: error handling frame for run_id=%s", run_id)

        reader = asyncio.create_task(handle_incoming())
        try:
            while True:
                item = await outbound.get()
                yield item
        finally:
            reader.cancel()

    async def _claim_run(self, run_id: str, outbound: asyncio.Queue) -> None:
        state = broker.get(run_id)
        if state is None:
            logger.warning("AgentSession: claim for unknown run_id=%s", run_id)
            return
        async with SessionLocal() as session:
            await repo.mark_run_status(session, run_id, "running")
        await outbound.put(
            souk_pb2.AgentEventEnvelope(
                run_id=run_id,
                agent_name=state.agent_name,
                json_payload=json.dumps(state.input_json),
            )
        )

    async def _relay_event(self, run_id: str, json_payload: str) -> None:
        # Persist *before* relaying: if souk crashes between the two, the
        # live caller must not end up having seen an event that was never
        # durably recorded. If the persist raises, this event is simply
        # never delivered — surfaced as "AgentSession: error handling
        # frame" by the caller in handle_incoming(), not swallowed.
        event = json.loads(json_payload)
        if is_pause_event(event):
            # Remembered for _finish_run, which decides the run's final
            # status once the stream actually ends — a pause event isn't
            # itself a stream terminator (see souk/pause.py).
            state = broker.get(run_id)
            if state is not None:
                state.pause_payload = event.get("value") or {}
        seq = broker.next_seq(run_id)
        async with SessionLocal() as session:
            await repo.append_run_event(session, run_id, seq, event)
            # Marks the run as still making progress — see
            # repo.fail_stalled_runs, which would otherwise eventually
            # treat a quiet-but-alive run as abandoned.
            await repo.touch_run_activity(session, run_id)
            await session.commit()
        await broker.deliver_event(run_id, event)

    async def _finish_run(self, run_id: str, outbound: asyncio.Queue) -> None:
        state = broker.get(run_id)
        async with SessionLocal() as session:
            if state is not None and state.pause_payload is not None:
                await repo.mark_run_status(session, run_id, "input-required", metadata=state.pause_payload)
            else:
                await repo.mark_run_status(session, run_id, "completed")
        await broker.close_run(run_id)
        await outbound.put(
            souk_pb2.AgentEventEnvelope(
                run_id=run_id, agent_name=state.agent_name if state else "", ack=True
            )
        )
        # A plain completion (not a pause) may be exactly what an outer
        # thread's run was waiting on — see souk/pause.py's
        # waitingOnThreadId. Best-effort: this only ever matters for
        # sub-agent-style delegation, so a failure here is logged, not
        # allowed to break the run that just legitimately finished.
        if state is not None and state.pause_payload is None:
            try:
                await self._resume_parent_run_if_waiting(state.thread_id)
            except Exception:
                logger.exception("failed to check/resume parent run waiting on thread=%s", state.thread_id)

    async def _resume_parent_run_if_waiting(self, child_thread_id: str) -> None:
        async with SessionLocal() as session:
            parent_run = await repo.find_parent_run_waiting_on(session, child_thread_id)
            if parent_run is None:
                return
            parent_thread_id = parent_run["thread_id"]
            child_messages = await repo.get_thread_messages(session, child_thread_id)
            result_text = _last_assistant_text(child_messages)
            forwarded_props = {
                "resume": {"waitingOnThreadId": child_thread_id, "result": result_text}
            }
            created = await repo.create_run(
                session,
                parent_thread_id,
                parent_run["agent_name"],
                parent_run["protocol"],
                {"resume": forwarded_props["resume"]},
                metadata={"resumedFrom": child_thread_id},
            )
            new_run_id = created["run_id"]
            # Closes out the paused run now that its successor exists —
            # otherwise both rows briefly look 'active' and
            # get_active_run_for_thread could still surface the stale
            # paused one once this new run itself finishes.
            await repo.mark_run_resumed(session, parent_run["run_id"], new_run_id)
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
            new_run_id, parent_run["agent_name"], parent_thread_id, input_json, parent_run["protocol"]
        )
        logger.info(
            "auto-resumed run=%s on thread=%s after child thread=%s completed",
            new_run_id,
            parent_thread_id,
            child_thread_id,
        )


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
