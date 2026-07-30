"""Agent-side SDK: registers a batch of AG-UI-shaped agents with a souk,
polls for work, and drives every discovered run through one persistent,
multiplexed gRPC AgentSession stream — opened once and kept open for the
client's whole lifetime, not reopened per run (see proto/souk.proto).

Agents are agnostic to pydantic-ai or any other implementation — the only
requirement is `run_stream(RunAgentInput dict) -> AsyncIterator[dict]`
yielding AG-UI event dicts, which is exactly what pydantic-ai's AG-UI
adapter already produces.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import grpc
import httpx

from souk_agent_sdk.grpc_gen import souk_pb2, souk_pb2_grpc

logger = logging.getLogger("souk_agent_sdk")

RunStream = Callable[[dict[str, Any]], AsyncIterator[dict[str, Any]]]


@dataclass
class AgentHandle:
    name: str
    run_stream: RunStream
    description: str = ""
    agent_card_extra: dict[str, Any] = field(default_factory=dict)


class SoukAgentClient:
    def __init__(
        self,
        souk_http_url: str,
        souk_grpc_url: str,
        agents: list[AgentHandle],
        sdk_client_id: str | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        self.souk_http_url = souk_http_url.rstrip("/")
        self.souk_grpc_url = souk_grpc_url
        self.agents: dict[str, AgentHandle] = {a.name: a for a in agents}
        self.sdk_client_id = sdk_client_id or f"sdk_{secrets.token_hex(8)}"
        self.poll_interval = poll_interval

        self._channel: grpc.aio.Channel | None = None
        self._stub: souk_pb2_grpc.SoukAgentGatewayStub | None = None
        self._session_call = None
        self._outbound: asyncio.Queue = asyncio.Queue()
        self._inboxes: dict[str, asyncio.Queue] = {}
        self._in_flight: set[asyncio.Task] = set()

    async def register(self) -> None:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.souk_http_url}/agents/register",
                json={
                    "sdk_client_id": self.sdk_client_id,
                    "agents": [
                        {
                            "name": a.name,
                            "description": a.description,
                            "agent_card_extra": a.agent_card_extra,
                        }
                        for a in self.agents.values()
                    ],
                },
            )
            resp.raise_for_status()
        logger.info("registered %d agent(s) as sdk_client_id=%s", len(self.agents), self.sdk_client_id)

    async def run_forever(self) -> None:
        await self.register()
        self._channel = grpc.aio.insecure_channel(self.souk_grpc_url)
        self._stub = souk_pb2_grpc.SoukAgentGatewayStub(self._channel)
        self._session_call = self._stub.AgentSession()

        writer = asyncio.create_task(self._write_loop())
        reader = asyncio.create_task(self._read_loop())
        try:
            while True:
                await self._poll_once()
                await asyncio.sleep(self.poll_interval)
        finally:
            writer.cancel()
            reader.cancel()
            for task in self._in_flight:
                task.cancel()
            await self._channel.close()

    async def _write_loop(self) -> None:
        # Single writer serializing all outbound envelopes onto the one
        # shared AgentSession stream, since concurrent runs each queue
        # writes here rather than writing to the stream directly.
        while True:
            envelope = await self._outbound.get()
            await self._session_call.write(envelope)

    async def _read_loop(self) -> None:
        # Demultiplexes inbound envelopes (RunAgentInput deliveries and
        # acks) by run_id into each in-flight run's inbox.
        async for envelope in self._session_call:
            inbox = self._inboxes.get(envelope.run_id)
            if inbox is not None:
                await inbox.put(envelope)
            else:
                logger.warning("AgentSession: frame for unknown/finished run_id=%s", envelope.run_id)

    async def _poll_once(self) -> None:
        assert self._stub is not None
        response = await self._stub.PollForWork(
            souk_pb2.PollRequest(agent_names=list(self.agents.keys()))
        )
        for pending in response.pending:
            task = asyncio.create_task(self._handle_run(pending.run_id, pending.agent_name))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)

    async def _handle_run(self, run_id: str, agent_name: str) -> None:
        handle = self.agents.get(agent_name)
        if handle is None:
            logger.warning("PollForWork returned run for unknown local agent '%s'", agent_name)
            return

        inbox: asyncio.Queue = asyncio.Queue()
        self._inboxes[run_id] = inbox
        try:
            await self._outbound.put(souk_pb2.AgentEventEnvelope(run_id=run_id, agent_name=agent_name))
            first = await inbox.get()
            run_input = json.loads(first.json_payload)

            async for event in handle.run_stream(run_input):
                await self._outbound.put(
                    souk_pb2.AgentEventEnvelope(
                        run_id=run_id, agent_name=agent_name, json_payload=json.dumps(event)
                    )
                )
        except Exception:
            logger.exception("run %s for agent '%s' failed", run_id, agent_name)
        finally:
            await self._outbound.put(
                souk_pb2.AgentEventEnvelope(run_id=run_id, agent_name=agent_name, end_of_stream=True)
            )
            ack = await inbox.get()
            if not ack.ack:
                logger.warning("run %s: expected ack frame, got something else", run_id)
            self._inboxes.pop(run_id, None)
