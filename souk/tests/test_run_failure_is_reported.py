
from __future__ import annotations

import asyncio

from souk_provider_sdk import InProcessProvider, ProviderIdentity, ProviderRuntime

import pytest

from souk import repo
from souk.config import CoreSettings
from souk.broker import RunBroker
from souk.core import Souk



# Local rather than imported from another test module: these used to come
# from test_in_process_worker, which was deleted with the mechanism it tested,
# and took this file's collection down with it.
async def _until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def _register(souk, *names: str):
    identity = ProviderIdentity.generate()
    signature, timestamp = identity.sign_registration(list(names))
    registration = await souk.register_agents(
        identity.public_key, signature, timestamp, [{"name": n} for n in names]
    )
    return registration, identity




@pytest.fixture
async def brisk(settings: CoreSettings):
    """Its own souk, because these tests want the health sweeps' own timing.

    Started, not merely constructed: the broker's loop is the only thing that
    hands a run to anybody, so a souk that was never started accepts runs and
    dispatches none of them.
    """
    souk = Souk(settings)
    await souk.start()
    runtimes: list[ProviderRuntime] = []

    async def _attach(identity, provider, names):
        runtime = ProviderRuntime(identity, provider)
        runtimes.append(runtime)
        runtime.start()
        await souk.attach_provider(InProcessProvider(souk, runtime), list(names))
        return runtime

    souk.attach = _attach
    try:
        yield souk
    finally:
        for runtime in runtimes:
            await runtime.aclose(cancel_in_flight=True)
        await souk.aclose()


async def test_a_provider_that_raises_reaches_the_caller_as_run_error(brisk):
    registration, identity = await _register(brisk, "explodes")
    agent_id = registration.agents["explodes"]

    class Exploding:
        async def run_stream(self, agent_id: str, run_input: dict):
            raise KeyError("token")
            yield

    await brisk.attach(identity, Exploding(), [agent_id.name])
    handle = await brisk.start_run(agent_id, {"messages": []})

    events = [e async for e in handle.events()]

    assert [e["type"] for e in events] == ["RUN_ERROR"]
    assert events[0]["code"] == "provider_stream_ended_without_finishing"

    await _until(lambda: handle.run_id not in brisk.active_runs())
    async with brisk.session() as session:
        stored = await repo.get_run(session, handle.run_id)
        persisted = await repo.get_run_events(session, handle.run_id)
    assert stored.status == "failed"
    assert [e["type"] for e in persisted] == ["RUN_ERROR"]


async def test_a_provider_that_reports_its_own_failure_is_not_corrected(brisk):
    registration, identity = await _register(brisk, "polite")
    agent_id = registration.agents["polite"]

    class ReportsItsOwn:
        async def run_stream(self, agent_id: str, run_input: dict):
            yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
            yield {"type": "RUN_ERROR", "message": "upstream model refused the request"}

    await brisk.attach(identity, ReportsItsOwn(), [agent_id.name])
    handle = await brisk.start_run(agent_id, {"messages": []})

    events = [e async for e in handle.events()]

    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1]["message"] == "upstream model refused the request"

    await _until(lambda: handle.run_id not in brisk.active_runs())
    async with brisk.session() as session:
        assert (await repo.get_run(session, handle.run_id)).status == "failed"


async def test_a_cancelled_run_gets_no_run_error(brisk):
    registration, identity = await _register(brisk, "stoppable")
    agent_id = registration.agents["stoppable"]
    started = asyncio.Event()

    class WaitsForever:
        async def run_stream(self, agent_id: str, run_input: dict):
            yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
            started.set()
            await asyncio.Event().wait()

    await brisk.attach(identity, WaitsForever(), [agent_id.name])
    handle = await brisk.start_run(agent_id, {"messages": []})

    stream = handle.events()
    assert (await stream.__anext__())["type"] == "RUN_STARTED"
    await started.wait()
    handle.cancel()

    assert [e["type"] async for e in stream] == []

    await _until(lambda: handle.run_id not in brisk.active_runs())
    async with brisk.session() as session:
        assert (await repo.get_run(session, handle.run_id)).status == "cancelled"


async def test_a_run_nobody_ever_comes_for_is_given_up_on(settings: CoreSettings):
    """The caller is watching a run for an agent no provider is serving. souk
    cannot make one appear, and leaving the caller to wait forever is the
    failure mode issue #37 was about — so the broker gives up, and says why.

    Its own souk with its own broker, because how long a run may go unwanted
    is the broker's number: a copy of it in settings is a copy that can
    disagree with the one actually used, which it once did.
    """
    souk = Souk(settings, broker=RunBroker(queued_timeout_seconds=0.05))
    await souk.start()
    try:
        _registration, identity = await _register(souk, "unserved")
        agent = _registration.agents["unserved"]

        handle = await souk.start_run(agent, {"messages": []})
        # Bounded: if the broker stops giving up on unwanted runs, this stream
        # never ends, and an unbounded read turns that into a hung suite
        # instead of a failing test. Measured — it hung for 200s.
        async with asyncio.timeout(5):
            [_ async for _ in handle.events()]

        await _until(lambda: handle.run_id not in souk.active_runs())
        run = await souk.get_run(handle.run_id)
        assert run.status == "failed"
        assert run.metadata["failureReason"] == "no_provider_took_it"
    finally:
        await souk.aclose()
