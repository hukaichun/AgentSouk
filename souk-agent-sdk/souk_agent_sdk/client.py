"""Agent-side SDK: a worker loop against a souk.

The loop is the whole thing, and it is the same one souk runs for an agent
hosted in its own process (souk/worker.py) — this SDK is that loop with a
wire in the middle:

    claim runs (with their input)  ->  run each one  ->  push its events back

Registers a batch of AG-UI-shaped agents, long-polls for work while idle,
and — only once there's actually work to do — opens one persistent,
multiplexed gRPC AgentSession stream shared by every run currently in
flight, not reopened per run (see proto/souk.proto). The stream carries this
worker's output; the runs themselves were already claimed, with their input,
over PollForWork. It closes again once no more work is queued and nothing is
left to report, so an idle provider holds no persistent stream at all, just
a periodic long-polling PollForWork call (see _run_connection/_active_session).

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

        # Belongs to the *current* connection attempt, replaced by
        # _active_session() on every (re)connect.
        self._session_call = None
        # Frames waiting to go out. Deliberately *not* per connection: a run
        # is addressed by run_id rather than by the stream it arrived on, so
        # events (and the end_of_stream) of a run cut short by a dropped
        # connection are still worth sending, and go out on the next one.
        self._outbound: asyncio.Queue = asyncio.Queue()
        # Runs currently being executed, by run_id. This is the worker's own
        # bookkeeping — what it has in flight (for max_claim) and what to
        # stop when souk asks. It replaces a queue-per-run inbox that existed
        # only to deliver each run its input; claiming carries that now.
        self._in_flight: dict[str, asyncio.Task] = {}

    async def register(self) -> None:
        names = [a.name for a in self.agents.values()]
        timestamp = int(time.time())
        payload = registration_signing_payload(names, timestamp)
        async with httpx.AsyncClient(verify=self.ca_cert_path or True) as client:
            resp = await client.post(
                f"{self.souk_http_url}/agents/register",
                json={
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
        logger.info(
            "registered %d agent(s) as provider %s", len(self.agents), public_key_hex(self._identity)
        )

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
                # An empty outbound queue is not a given even with no work:
                # a run cut short by a previous connection dropping still
                # has its end_of_stream to deliver, and souk keeps that run
                # 'running' until it arrives (it records no outcome it
                # hasn't observed). Open a stream to flush it rather than
                # sitting on it until the next run happens to come along.
                if pending or not self._outbound.empty():
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
        """Opens AgentSession, runs `initial_pending`, and keeps checking for
        more work as capacity frees up — closing the stream (returning to the
        caller's idle long-poll loop) only once a check comes back empty,
        rather than tearing the stream down and making souk wait for a fresh
        connection when there's already more queued for this provider.
        """
        auth_metadata = (("authorization", self._session_token),)
        self._session_call = stub.AgentSession(metadata=auth_metadata)

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
                        # Everything queued has to be *written*, not merely
                        # queued, before this stream goes away: closing with
                        # a frame still in the writer's hands loses it, and
                        # the frame most likely to be sitting there is a
                        # run's end_of_stream — the one souk is waiting on
                        # to decide that run's outcome.
                        await self._flush_outbound(writer)
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
            # This stream is going away, so nothing this provider produces
            # can reach souk until a new one is open. Stop the runs rather
            # than let them keep spending on an LLM whose output is going
            # nowhere; their end_of_stream frames queue on the (connection-
            # independent) outbound queue and go out on the next stream, so
            # souk still hears how they ended.
            for task in list(self._in_flight.values()):
                task.cancel()

    def _dispatch(self, pending: list[Any]) -> None:
        for p in pending:
            task = asyncio.create_task(
                self._handle_run(p.run_id, p.agent_id, json.loads(p.json_payload))
            )
            self._in_flight[p.run_id] = task
            task.add_done_callback(
                lambda _task, run_id=p.run_id: self._in_flight.pop(run_id, None)
            )

    async def _write_loop(self) -> None:
        # Single writer serializing all outbound envelopes onto the one
        # shared AgentSession stream, since concurrent runs each queue
        # writes here rather than writing to the stream directly.
        #
        # task_done in a finally is what makes _flush_outbound's join() mean
        # "actually written" rather than "handed to the writer". A frame
        # whose write raised is gone (this stream is dead by then, and there
        # is no acknowledgement to retry against — see proto/souk.proto's
        # reserved field 5); anything still queued behind it survives, in
        # order, and goes out on the next connection.
        while True:
            envelope = await self._outbound.get()
            try:
                await self._session_call.write(envelope)
            finally:
                self._outbound.task_done()

    async def _flush_outbound(self, writer: asyncio.Task) -> None:
        """Wait for the writer to work through everything queued — or for
        the writer to die, whichever happens first. Waiting on the queue
        alone would hang forever on a broken stream."""
        drained = asyncio.create_task(self._outbound.join())
        done, _pending = await asyncio.wait({drained, writer}, return_when=asyncio.FIRST_COMPLETED)
        if drained not in done:
            drained.cancel()

    async def _read_loop(self) -> None:
        # souk sends exactly one kind of frame: "please stop this run". This
        # worker complies, by cancelling that run's task — which delivers
        # CancelledError into run_stream's *current* await, not merely
        # between yields, so an in-flight LLM or tool call is really
        # interrupted rather than paid for and discarded.
        #
        # Complying is this worker's choice. souk asked; it did not decide.
        # Whatever the run emits between now and its end_of_stream is real
        # output that souk persists and relays like any other, and a worker
        # that ignored this and finished normally would have its run
        # recorded as completed (see proto/souk.proto's `cancel`).
        async for envelope in self._session_call:
            task = self._in_flight.get(envelope.run_id)
            if task is None:
                # A run that finished while the request was in flight.
                logger.debug("AgentSession: frame for unknown/finished run_id=%s", envelope.run_id)
            elif envelope.cancel:
                logger.info("run %s: souk asked it to stop", envelope.run_id)
                task.cancel()
            else:
                logger.warning("run %s: unexpected frame from souk, ignoring", envelope.run_id)

    async def _handle_run(self, run_id: str, agent_id: str, run_input: dict[str, Any]) -> None:
        """One claimed run: feed the input to the agent, push every event
        back. The input arrived with the claim, so there is nothing to wait
        for here — under the old contract this had to announce itself and
        block on souk sending the input back, which is what a cancel
        arriving first could strand.
        """
        handle = self._handle_by_id.get(agent_id)
        if handle is None:
            logger.warning("PollForWork returned run for unknown local agent_id '%s'", agent_id)
            return

        outbound = self._outbound
        try:
            async for event in handle.run_stream(run_input):
                await outbound.put(
                    souk_pb2.AgentEventEnvelope(
                        run_id=run_id, agent_id=agent_id, json_payload=json.dumps(event)
                    )
                )
        except asyncio.CancelledError:
            logger.info("run %s: stopped", run_id)
            raise
        except Exception:
            logger.exception("run %s for agent_id '%s' failed", run_id, agent_id)
        finally:
            # end_of_stream is the last word on this run — souk sends
            # nothing back for it (see proto/souk.proto's reserved field 5).
            # This used to be followed by a 5s wait for an `ack` envelope
            # confirming souk had persisted everything; it was removed
            # because there was no action to take on it either way. This
            # code has already produced and discarded the run's events, so a
            # failed persist could only be logged, never retried — while the
            # wait cost a round trip on every run, and a full 5s stall plus
            # a misleading "connection likely lost" warning whenever the
            # frame went astray.
            #
            # put_nowait, not await put: this also runs while unwinding a
            # cancellation, and an await here would be interrupted before
            # the frame was ever queued — leaving souk holding a run whose
            # stream never ended, until its stall sweep noticed.
            outbound.put_nowait(
                souk_pb2.AgentEventEnvelope(run_id=run_id, agent_id=agent_id, end_of_stream=True)
            )
