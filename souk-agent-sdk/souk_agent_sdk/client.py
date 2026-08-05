"""Agent-side SDK: registers a batch of AG-UI-shaped agents with a souk,
long-polls for work while idle, and — only once there's actually work to
do — opens one persistent, multiplexed gRPC AgentSession stream shared by
every run currently in flight, not reopened per run (see proto/souk.proto).
The stream closes again once no more work is queued, so an idle provider
holds no persistent stream at all, just a periodic long-polling PollForWork
call (see _run_connection/_active_session).

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
import contextlib
import json
import logging
import secrets
import time
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
        long_poll_seconds: float = 25.0,
        reconnect_delay: float = 2.0,
        max_concurrent_runs: int | None = None,
        identity_key_path: str = "souk_identity.key",
        ca_cert_path: str | None = None,
        provider_name: str | None = None,
    ) -> None:
        self.souk_http_url = souk_http_url.rstrip("/")
        # Optional storefront label for this provider's public_key,
        # shown when souk-directory groups agents by provider — see
        # souk/db.py's providers table. Purely descriptive; unset means
        # "didn't say", not "cleared" (souk leaves any previously-set
        # name alone rather than blanking it — see repo.register_agents).
        self.provider_name = provider_name
        self.souk_grpc_url = souk_grpc_url
        # Path to a CA (or self-signed) cert this souk's TLS certificate
        # should be verified against — leave unset only for plaintext
        # development. Verifying against a specific file here (rather
        # than the system trust store) is what makes this provider
        # actually confirm it's talking to *this* souk and not an
        # impostor on the network; skipping verification (or not using
        # TLS at all) means it can't tell the difference. See
        # scripts/gen_dev_tls_cert.py and souk.config's http_tls_*/
        # grpc_tls_* settings.
        self.ca_cert_path = ca_cert_path
        self.agents: dict[str, AgentHandle] = {a.name: a for a in agents}
        # Populated by register() from the {name: agent_id} map souk
        # returns — dispatch (PollForWork/AgentSession) is keyed by
        # agent_id, not name, since name is no longer a unique routing key
        # (see souk/db.py's UNIQUE(public_key, name)). Empty until the
        # first successful registration.
        self._handle_by_id: dict[str, AgentHandle] = {}
        self.sdk_client_id = sdk_client_id or f"sdk_{secrets.token_hex(8)}"
        # How often to re-check for more work while already actively
        # processing runs on an open AgentSession stream (see
        # _active_session) — a plain sleep between top-up checks, not a
        # long-poll, since the stream being open already means there's
        # something to do.
        self.poll_interval = poll_interval
        # How long an idle PollForWork call (no AgentSession stream open —
        # see _run_connection) is allowed to block waiting for work before
        # souk responds empty and this SDK asks again. Long-polling here
        # is what lets an idle provider react to new work in roughly the
        # time it takes one round trip, without holding a permanent
        # stream open the whole time it has nothing to do.
        self.long_poll_seconds = long_poll_seconds
        self.reconnect_delay = reconnect_delay
        # This provider's identity to any souk it connects to — see
        # souk_agent_sdk.identity. Persisted to disk: restarting this
        # process must keep resolving to the same agent_ids it registered
        # before, which only works if it keeps using the same keypair (see
        # souk_agent_sdk.identity's module docstring).
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
        timestamp = int(time.time())
        payload = registration_signing_payload(self.sdk_client_id, names, timestamp)
        async with httpx.AsyncClient(verify=self.ca_cert_path or True) as client:
            resp = await client.post(
                f"{self.souk_http_url}/agents/register",
                json={
                    "sdk_client_id": self.sdk_client_id,
                    "public_key": public_key_hex(self._identity),
                    "signature": sign(self._identity, payload),
                    "timestamp": timestamp,
                    "provider_name": self.provider_name,
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
        body = resp.json()
        self._session_token = body["session_token"]
        agent_ids: dict[str, str] = body["agent_ids"]
        self._handle_by_id = {
            agent_ids[name]: handle for name, handle in self.agents.items() if name in agent_ids
        }
        logger.info("registered %d agent(s) as sdk_client_id=%s", len(self.agents), self.sdk_client_id)

    async def run_forever(self) -> None:
        """Keeps a connection to souk alive indefinitely — reconnecting
        with a fixed delay if a stream or poll call ever errors out, so a
        transient network blip doesn't permanently stop this provider from
        checking for and responding to souk's work. Re-registers on every
        (re)connect, not just the first — that's also how the bearer token
        gets refreshed before it expires (see
        souk.identity.SESSION_TOKEN_TTL_SECONDS), without a separate
        renewal mechanism.
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
        """Alternates between two phases on one gRPC channel, kept open
        across both so idle cycles don't pay a fresh handshake: an idle
        phase that only long-polls PollForWork (no AgentSession stream —
        see _poll_for_work), and an active phase that opens AgentSession
        to actually claim and run work, staying open only for as long as
        there's work to do (see _active_session). This keeps this
        provider's connection count proportional to whether it's actually
        working, not permanently open regardless of load.
        """
        if self.ca_cert_path:
            with open(self.ca_cert_path, "rb") as f:
                credentials = grpc.ssl_channel_credentials(root_certificates=f.read())
            channel = grpc.aio.secure_channel(self.souk_grpc_url, credentials)
        else:
            channel = grpc.aio.insecure_channel(self.souk_grpc_url)
        stub = souk_pb2_grpc.SoukAgentGatewayStub(channel)
        try:
            while True:
                pending = await self._poll_for_work(stub, wait_seconds=self.long_poll_seconds)
                if pending:
                    await self._active_session(stub, pending)
        finally:
            await channel.close()

    async def _poll_for_work(
        self, stub: souk_pb2_grpc.SoukAgentGatewayStub, wait_seconds: float = 0
    ) -> list[Any]:
        # Field left unset entirely when there's no configured limit —
        # PollRequest.max_claim distinguishes "unset" (unlimited) from an
        # explicit 0 ("no spare capacity right now"), so this must not
        # send 0 as a stand-in for "unlimited".
        kwargs: dict[str, Any] = {"agent_ids": list(self._handle_by_id.keys())}
        max_claim = None
        if self.max_concurrent_runs is not None:
            max_claim = max(0, self.max_concurrent_runs - len(self._in_flight))
            kwargs["max_claim"] = max_claim
        # No point asking souk to hold the call open when we've already
        # told it we have zero spare capacity — nothing souk-side will
        # change that; capacity only frees up here as in-flight runs
        # finish (souk enforces the same skip, see grpc_server.PollForWork).
        if wait_seconds > 0 and max_claim != 0:
            kwargs["wait_seconds"] = int(wait_seconds)
        response = await stub.PollForWork(
            souk_pb2.PollRequest(**kwargs), metadata=(("authorization", self._session_token),)
        )
        return list(response.pending)

    async def _active_session(
        self, stub: souk_pb2_grpc.SoukAgentGatewayStub, initial_pending: list[Any]
    ) -> None:
        """Opens AgentSession, claims/processes `initial_pending`, and
        keeps checking for more work as capacity frees up — closing the
        stream (returning to the caller's idle long-poll loop) only once a
        check comes back empty, rather than tearing the stream down and
        making souk wait for a fresh connection when there's already more
        queued for this provider.
        """
        auth_metadata = (("authorization", self._session_token),)
        self._session_call = stub.AgentSession(metadata=auth_metadata)
        self._outbound = asyncio.Queue()
        self._inboxes = {}

        writer = asyncio.create_task(self._write_loop())
        reader = asyncio.create_task(self._read_loop())
        try:
            self._dispatch(initial_pending)
            while True:
                # The writer/reader tasks only ever exit via an exception
                # (dead stream) — surface that here so it triggers a
                # reconnect instead of this loop spinning obliviously
                # against a stream that's no longer delivering anything.
                if writer.done():
                    writer.result()
                if reader.done():
                    reader.result()
                if not self._in_flight:
                    # Drained — one last non-blocking check before giving
                    # up this stream.
                    more = await self._poll_for_work(stub)
                    if not more:
                        return
                    self._dispatch(more)
                    continue
                await asyncio.sleep(self.poll_interval)
                more = await self._poll_for_work(stub)
                if more:
                    self._dispatch(more)
        finally:
            writer.cancel()
            reader.cancel()
            # This stream is going away — any run still waiting on it can
            # never receive its input or an ack again. Cancel them rather
            # than let them hang forever; _handle_run's cleanup is
            # bounded (see its finally block) regardless of why it woke up.
            for task in list(self._in_flight):
                task.cancel()

    def _dispatch(self, pending: list[Any]) -> None:
        for p in pending:
            task = asyncio.create_task(self._handle_run(p.run_id, p.agent_id))
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)

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

    async def _handle_run(self, run_id: str, agent_id: str) -> None:
        handle = self._handle_by_id.get(agent_id)
        if handle is None:
            logger.warning("PollForWork returned run for unknown local agent_id '%s'", agent_id)
            return

        inbox: asyncio.Queue = asyncio.Queue()
        self._inboxes[run_id] = inbox
        outbound = self._outbound
        try:
            await outbound.put(souk_pb2.AgentEventEnvelope(run_id=run_id, agent_id=agent_id))
            first = await inbox.get()
            run_input = json.loads(first.json_payload)

            async def consume() -> None:
                async for event in handle.run_stream(run_input):
                    await outbound.put(
                        souk_pb2.AgentEventEnvelope(
                            run_id=run_id, agent_id=agent_id, json_payload=json.dumps(event)
                        )
                    )

            async def watch_cancel() -> None:
                # souk's cancel envelope is the only thing that should show
                # up here while a run is in flight — the ack this run is
                # itself waiting for (below) arrives after end_of_stream,
                # by which point this watcher has already been cancelled.
                while True:
                    envelope = await inbox.get()
                    if envelope.cancel:
                        return
                    logger.warning("run %s: unexpected frame while running, ignoring", run_id)

            consumer = asyncio.create_task(consume())
            watcher = asyncio.create_task(watch_cancel())
            done, _pending = await asyncio.wait({consumer, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if consumer in done:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher
                consumer.result()
            else:
                # souk will never read another event for this run (its
                # consumer disconnected) — stop running run_stream instead
                # of paying for an LLM call/tool loop nobody will see.
                # Cancelling the task delivers CancelledError into
                # run_stream's *current* await, not just between yields,
                # so an in-flight LLM/tool call is actually interrupted.
                logger.info("run %s: cancelled by souk (consumer gone)", run_id)
                consumer.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await consumer
        except Exception:
            logger.exception("run %s for agent_id '%s' failed", run_id, agent_id)
        finally:
            # Best-effort: if this run was cancelled because the
            # connection died, outbound is a queue nobody's writer will
            # ever drain again and inbox will never receive an ack —
            # never let cleanup hang indefinitely on either.
            #
            # The inbox must stay registered in self._inboxes until the
            # ack wait below is actually over — _read_loop demultiplexes
            # purely by looking up self._inboxes[run_id], so popping it
            # first (as this used to do) meant souk's ack always arrived
            # to find no inbox left, got dropped as "frame for
            # unknown/finished run_id", and every single run paid a
            # guaranteed 5s stall plus a misleading "connection likely
            # lost" warning even on a perfectly healthy connection.
            try:
                await outbound.put(
                    souk_pb2.AgentEventEnvelope(
                        run_id=run_id, agent_id=agent_id, end_of_stream=True
                    )
                )
                ack = await asyncio.wait_for(inbox.get(), timeout=5.0)
                if not ack.ack:
                    logger.warning("run %s: expected ack frame, got something else", run_id)
            except asyncio.TimeoutError:
                logger.warning(
                    "run %s: no ack from souk within timeout (connection likely lost)", run_id
                )
            finally:
                self._inboxes.pop(run_id, None)
