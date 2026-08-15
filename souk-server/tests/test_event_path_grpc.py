"""What the gRPC transport keeps, and what it no longer keeps.

The pull model routed an event into a per-run queue inside this transport
before it ever reached the run's own pipeline. Nothing here holds per-run
state now: a frame goes straight to core by run_id, into the only table
there ever needed to be. These tests pin that, plus the one message souk
sends the other way (a cancel request) and what happens to a run when the
connection carrying it drops. See souk/tests/test_event_path.py for the core
half of the same path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk.identity import registration_signing_payload
from souk_server.grpc_gen import souk_pb2
from souk_server.grpc_server import SoukAgentGatewayServicer


async def _register(souk, *names: str):
    """Returns what souk issued and the public key that *is* this provider —
    which is what a claim is recorded against and what every reported event
    is checked against."""
    key = Ed25519PrivateKey.generate()
    public_key = key.public_key().public_bytes_raw().hex()
    timestamp = int(time.time())
    registration = await souk.register_agents(
        public_key,
        key.sign(registration_signing_payload(list(names), timestamp)).hex(),
        timestamp,
        [{"name": n} for n in names],
    )
    return registration, public_key


class _FakeContext:
    def __init__(self, token: str) -> None:
        self._token = token

    def invocation_metadata(self):
        return (("authorization", self._token),)


class _Session:
    """One open AgentSession, driven the way grpc would drive it: frames in
    on the request iterator, frames out collected off the response stream,
    and a `close()` that tears the call down mid-flight like a dropped
    connection does."""

    def __init__(self, servicer: SoukAgentGatewayServicer, context: _FakeContext) -> None:
        self._inbound: asyncio.Queue = asyncio.Queue()
        self.received: asyncio.Queue = asyncio.Queue()
        self._stream = servicer.AgentSession(self._requests(), context)
        self._task = asyncio.create_task(self._consume())

    async def _requests(self):
        while (envelope := await self._inbound.get()) is not None:
            yield envelope

    async def _consume(self) -> None:
        async for frame in self._stream:
            self.received.put_nowait(frame)

    def send(self, envelope) -> None:
        self._inbound.put_nowait(envelope)

    async def close(self) -> None:
        self._inbound.put_nowait(None)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


async def test_the_grpc_transport_keeps_no_state_per_run(souk):
    """The routing table that disappeared. Three runs in flight over one
    AgentSession, and the servicer's only registry is of open streams by
    client — no run_id appears anywhere in it, because events go straight
    to core by run_id instead of being demultiplexed here first.
    """
    registration, public_key = await _register(souk, "greeter")
    agent_id = registration.agent_ids["greeter"]
    servicer = SoukAgentGatewayServicer(souk)
    context = _FakeContext(registration.session_token)

    handles = [await souk.start_run(agent_id, {"messages": []}) for _ in range(3)]
    streams = {h.run_id: h.events() for h in handles}
    run_ids = sorted(h.run_id for h in handles)
    claimed = await servicer.PollForWork(
        souk_pb2.PollRequest(agent_ids=[agent_id]), context
    )
    assert sorted(p.run_id for p in claimed.pending) == run_ids
    # Claiming *is* the hand-over: the input comes back with the run, not
    # in a follow-up frame over the session.
    assert json.loads(claimed.pending[0].json_payload)["runId"] == claimed.pending[0].run_id

    session = _Session(servicer, context)
    try:
        for run_id in run_ids:
            session.send(
                souk_pb2.AgentEventEnvelope(
                    run_id=run_id,
                    agent_id=agent_id,
                    json_payload=json.dumps({"type": "RUN_STARTED", "runId": run_id}),
                )
            )
        for run_id in run_ids:
            async with asyncio.timeout(2):
                assert (await anext(streams[run_id]))["type"] == "RUN_STARTED"

        # Every run is live and being served over this one stream, and the
        # transport holds nothing about any of them.
        state = repr(servicer.__dict__) + repr(servicer._sessions.__dict__)
        assert not any(run_id in state for run_id in run_ids)
    finally:
        for run_id in run_ids:
            souk.finish_run(run_id, claimed_by=public_key)
        await session.close()


async def test_a_cancel_reaches_the_worker_holding_the_run(souk):
    """souk's one message to a worker. It goes to whichever stream that
    provider identity has open when souk asks — not to the connection the
    run was claimed on, which may not even have existed yet.
    """
    registration, public_key = await _register(souk, "greeter")
    agent_id = registration.agent_ids["greeter"]
    servicer = SoukAgentGatewayServicer(souk)
    context = _FakeContext(registration.session_token)

    handle = await souk.start_run(agent_id, {"messages": []})
    await servicer.PollForWork(souk_pb2.PollRequest(agent_ids=[agent_id]), context)

    # The session opens *after* the claim, which is the ordinary case: a
    # worker claims first and opens a stream only once it has work.
    session = _Session(servicer, context)
    await asyncio.sleep(0)

    souk.cancel_run(handle.run_id)
    async with asyncio.timeout(2):
        frame = await session.received.get()
    assert (frame.run_id, frame.cancel) == (handle.run_id, True)

    # A request, not a command: souk keeps reading, and the run is
    # 'cancelling' until its stream actually ends.
    async with asyncio.timeout(2):
        while (await souk.get_run(handle.run_id))["status"] != "cancelling":
            await asyncio.sleep(0.01)

    souk.finish_run(handle.run_id, claimed_by=public_key)
    await session.close()
    async with asyncio.timeout(2):
        while souk.broker.get(handle.run_id) is not None:
            await asyncio.sleep(0.01)
    assert (await souk.get_run(handle.run_id))["status"] == "cancelled"
    assert [e async for e in handle.events()] == []


@pytest.mark.parametrize("frame", ["events", "end_of_stream"])
async def test_a_connection_dropping_does_not_end_the_runs_it_carried(souk, frame):
    """souk records no outcome it hasn't observed, and a dropped TCP
    connection is not one. A worker addresses a run by id, so it may
    reconnect and report the rest — including how it ended — on a new
    stream. This used to synthesise an ending for every run on the
    connection the moment it closed.
    """
    registration, public_key = await _register(souk, "greeter")
    agent_id = registration.agent_ids["greeter"]
    servicer = SoukAgentGatewayServicer(souk)
    context = _FakeContext(registration.session_token)

    handle = await souk.start_run(agent_id, {"messages": []})
    await servicer.PollForWork(souk_pb2.PollRequest(agent_ids=[agent_id]), context)

    session = _Session(servicer, context)
    session.send(
        souk_pb2.AgentEventEnvelope(
            run_id=handle.run_id,
            agent_id=agent_id,
            json_payload=json.dumps({"type": "RUN_STARTED", "runId": handle.run_id}),
        )
    )
    await asyncio.sleep(0)
    await session.close()  # the connection drops mid-run

    # Still dispatching it: the run did not end because a connection did.
    async with asyncio.timeout(2):
        while not await souk.get_run_events(handle.run_id):
            await asyncio.sleep(0.01)
    assert souk.broker.get(handle.run_id) is not None
    assert (await souk.get_run(handle.run_id))["status"] == "running"

    # A second stream from the same worker finishes it, and souk accepts it
    # because the run is addressed by id and held by this identity.
    second = souk_pb2.AgentEventEnvelope(run_id=handle.run_id, agent_id=agent_id, end_of_stream=True)
    if frame == "events":
        second = souk_pb2.AgentEventEnvelope(
            run_id=handle.run_id,
            agent_id=agent_id,
            json_payload=json.dumps({"type": "RUN_FINISHED", "runId": handle.run_id}),
        )
    resumed = _Session(servicer, context)
    resumed.send(second)

    if frame == "events":
        async with asyncio.timeout(2):
            while not any(
                e["type"] == "RUN_FINISHED" for e in await souk.get_run_events(handle.run_id)
            ):
                await asyncio.sleep(0.01)
        souk.finish_run(handle.run_id, claimed_by=public_key)
    async with asyncio.timeout(2):
        while souk.broker.get(handle.run_id) is not None:
            await asyncio.sleep(0.01)
    await resumed.close()
    # A run whose worker reported RUN_FINISHED completed; one that only
    # reported the end of its stream stopped without finishing, and souk
    # never asked it to — so it failed. Both are read off what arrived.
    expected = "completed" if frame == "events" else "failed"
    assert (await souk.get_run(handle.run_id))["status"] == expected
