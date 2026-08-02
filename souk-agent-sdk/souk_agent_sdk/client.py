"""Agent-side SDK: registers a batch of AG-UI-shaped agents with a souk,
polls for work, and drives every discovered run through one persistent,
multiplexed gRPC AgentSession stream — opened once per connection and kept
open for as long as the connection lasts, not reopened per run (see
proto/souk.proto).

This SDK is a convenience client, not the protocol itself — anything that
speaks proto/souk.proto's gRPC contract directly (in any language) is an
equally valid souk provider; souk never special-cases this implementation.

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
from souk_agent_sdk.identity import load_or_create_identity, public_key_hex, registration_signing_payload, sign

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
        reconnect_delay: float = 2.0,
        max_concurrent_runs: int | None = None,
        identity_key_path: str = "souk_identity.key",
    ) -> None:
        self.souk_http_url = souk_http_url.rstrip("/")
        self.souk_grpc_url = souk_grpc_url
        self.agents: dict[str, AgentHandle] = {a.name: a for a in agents}
        self.sdk_client_id = sdk_client_id or f"sdk_{secrets.token_hex(8)}"
        self.poll_interval = poll_interval
        self.reconnect_delay = reconnect_delay
        # This provider's identity to any souk it connects to — see
        # souk_agent_sdk.identity. Persisted to disk: restarting this
        # process must keep proving ownership of the same agent names,
        # which only works if it keeps using the same keypair.
        self._identity = load_or_create_identity(identity_key_path)
        self._session_token: str | None = None
        # How many runs this provider will claim at once, across all its
        # agents combined. None means unlimited — souk hands out its
        # whole backlog on every poll (the old, pre-throttling behavior).
        # Set this to your real concurrency limit so souk knows to leave
        # the rest queued instead of dumping more than you can actually
        # process; see PollRequest.max_claim.
        self.max_concurrent_runs = max_concurrent_runs

        # All of these belong to the *current* connection attempt and are
        # replaced wholesale by _run_connection() on every (re)connect.
        self._session_call = None
        self._outbound: asyncio.Queue = asyncio.Queue()
        self._inboxes: dict[str, asyncio.Queue] = {}
        self._in_flight: set[asyncio.Task] = set()

    async def register(self) -> None:
        names = [a.name for a in self.agents.values()]
        payload = registration_signing_payload(self.sdk_client_id, names)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.souk_http_url}/agents/register",
                json={
                    "sdk_client_id": self.sdk_client_id,
                    "public_key": public_key_hex(self._identity),
                    "signature": sign(self._identity, payload),
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
        self._session_token = resp.json()["session_token"]
        logger.info("registered %d agent(s) as sdk_client_id=%s", len(self.agents), self.sdk_client_id)

    async def run_forever(self) -> None:
        """Keeps a connection to souk alive indefinitely — reconnecting
        with a fixed delay if the AgentSession stream or the poll loop
        itself ever errors out, so a transient network blip doesn't
        permanently stop this provider from checking for and responding
        to souk's work. Re-registers on every (re)connect, not just the
        first — that's also how the bearer token gets refreshed before it
        expires (see souk.identity.SESSION_TOKEN_TTL_SECONDS), without a
        separate renewal mechanism.
        """
        while True:
            try:
                await self.register()
                await self._run_connection()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "souk connection lost; reconnecting in %.1fs", self.reconnect_delay
                )
            await asyncio.sleep(self.reconnect_delay)

    async def _run_connection(self) -> None:
        channel = grpc.aio.insecure_channel(self.souk_grpc_url)
        stub = souk_pb2_grpc.SoukAgentGatewayStub(channel)
        auth_metadata = (("authorization", self._session_token),)
        self._session_call = stub.AgentSession(metadata=auth_metadata)
        self._outbound = asyncio.Queue()
        self._inboxes = {}

        writer = asyncio.create_task(self._write_loop())
        reader = asyncio.create_task(self._read_loop())
        try:
            while True:
                await self._poll_once(stub)
                # The writer/reader tasks only ever exit via an exception
                # (dead stream) — surface that here so it triggers a
                # reconnect instead of the poll loop spinning obliviously
                # against a stream that's no longer delivering anything.
                if writer.done():
                    writer.result()
                if reader.done():
                    reader.result()
                await asyncio.sleep(self.poll_interval)
        finally:
            writer.cancel()
            reader.cancel()
            # This connection is gone — any run still waiting on it can
            # never receive its input or an ack again. Cancel them rather
            # than let them hang forever; _handle_run's cleanup is
            # bounded (see its finally block) regardless of why it woke up.
            for task in list(self._in_flight):
                task.cancel()
            await channel.close()

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

    async def _poll_once(self, stub: souk_pb2_grpc.SoukAgentGatewayStub) -> None:
        # Field left unset entirely when there's no configured limit —
        # PollRequest.max_claim distinguishes "unset" (unlimited) from an
        # explicit 0 ("no spare capacity right now"), so this must not
        # send 0 as a stand-in for "unlimited".
        kwargs: dict[str, Any] = {"agent_names": list(self.agents.keys())}
        if self.max_concurrent_runs is not None:
            kwargs["max_claim"] = max(0, self.max_concurrent_runs - len(self._in_flight))
        response = await stub.PollForWork(
            souk_pb2.PollRequest(**kwargs), metadata=(("authorization", self._session_token),)
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
        outbound = self._outbound
        try:
            await outbound.put(souk_pb2.AgentEventEnvelope(run_id=run_id, agent_name=agent_name))
            first = await inbox.get()
            run_input = json.loads(first.json_payload)

            async for event in handle.run_stream(run_input):
                await outbound.put(
                    souk_pb2.AgentEventEnvelope(
                        run_id=run_id, agent_name=agent_name, json_payload=json.dumps(event)
                    )
                )
        except Exception:
            logger.exception("run %s for agent '%s' failed", run_id, agent_name)
        finally:
            self._inboxes.pop(run_id, None)
            # Best-effort: if this run was cancelled because the
            # connection died, outbound is a queue nobody's writer will
            # ever drain again and inbox will never receive an ack —
            # never let cleanup hang indefinitely on either.
            try:
                await outbound.put(
                    souk_pb2.AgentEventEnvelope(
                        run_id=run_id, agent_name=agent_name, end_of_stream=True
                    )
                )
                ack = await asyncio.wait_for(inbox.get(), timeout=5.0)
                if not ack.ack:
                    logger.warning("run %s: expected ack frame, got something else", run_id)
            except asyncio.TimeoutError:
                logger.warning(
                    "run %s: no ack from souk within timeout (connection likely lost)", run_id
                )
