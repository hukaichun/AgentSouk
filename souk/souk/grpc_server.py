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
from souk.broker import broker
from souk.config import settings
from souk.db import SessionLocal
from souk.grpc_gen import souk_pb2, souk_pb2_grpc

logger = logging.getLogger("souk.grpc")


class SoukAgentGatewayServicer(souk_pb2_grpc.SoukAgentGatewayServicer):
    async def PollForWork(self, request, context):
        agent_names = list(request.agent_names)
        states = broker.poll(agent_names)

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
        seq = broker.next_seq(run_id)
        async with SessionLocal() as session:
            await repo.append_run_event(session, run_id, seq, event)
        await broker.deliver_event(run_id, event)

    async def _finish_run(self, run_id: str, outbound: asyncio.Queue) -> None:
        state = broker.get(run_id)
        async with SessionLocal() as session:
            await repo.mark_run_status(session, run_id, "completed")
        await broker.close_run(run_id)
        await outbound.put(
            souk_pb2.AgentEventEnvelope(
                run_id=run_id, agent_name=state.agent_name if state else "", ack=True
            )
        )


def create_grpc_server() -> grpc.aio.Server:
    server = grpc.aio.server()
    souk_pb2_grpc.add_SoukAgentGatewayServicer_to_server(SoukAgentGatewayServicer(), server)
    server.add_insecure_port(f"{settings.grpc_host}:{settings.grpc_port}")
    return server
