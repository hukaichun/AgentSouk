from __future__ import annotations

import asyncio

from funduq_provider_sdk import InProcessLink, ProviderIdentity, ProviderRuntime

import pytest

from funduq import repo
from funduq.config import CoreSettings
from funduq.broker import RunBroker
from funduq.core import Funduq


async def _until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


async def _register(funduq, *names: str):
    identity = ProviderIdentity.generate()
    signature, timestamp = identity.sign_registration(list(names))
    registration = await funduq.register_agents(
        identity.public_key, signature, timestamp, [{"name": n} for n in names]
    )
    return registration, identity


@pytest.fixture
async def brisk(settings: CoreSettings):
    funduq = Funduq(settings)
    await funduq.start()
    runtimes: list[ProviderRuntime] = []

    async def _attach(identity, provider, names):
        runtime = ProviderRuntime(identity, provider)
        runtimes.append(runtime)
        runtime.start()
        await funduq.attach_provider(InProcessLink(funduq, runtime), list(names))
        return runtime

    funduq.attach = _attach
    try:
        yield funduq
    finally:
        for runtime in runtimes:
            await runtime.aclose(cancel_in_flight=True)
        await funduq.aclose()


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


async def test_a_permanent_refusal_fails_the_run_with_the_providers_reason(brisk):
    from funduq_provider_sdk import Refusal

    registration, identity = await _register(brisk, "retired")
    agent_id = registration.agents["retired"]

    class Refuses:
        max_concurrent_runs = None

        def __init__(self) -> None:
            self.public_key = identity.public_key
            self.sign_connect = identity.sign_connect
            self.offers = 0

        async def deliver(self, run):
            self.offers += 1
            return Refusal("this agent was retired, run something newer")

        def cancel(self, run_id: str) -> None:
            pass

    link = Refuses()
    await brisk.attach_provider(link, [agent_id.name])
    handle = await brisk.start_run(agent_id, {"messages": []})

    events = [e async for e in handle.events()]
    assert [e["type"] for e in events] == ["RUN_ERROR"]
    assert events[0]["message"] == "this agent was retired, run something newer"

    await _until(lambda: handle.run_id not in brisk.active_runs())
    async with brisk.session() as session:
        stored = await repo.get_run(session, handle.run_id)
    assert stored.status == "failed"
    assert stored.metadata["failureReason"] == "this agent was retired, run something newer"
    assert link.offers == 1


async def test_a_malformed_event_fails_the_run_instead_of_relaying_garbage(brisk):
    registration, identity = await _register(brisk, "malformed")
    agent_id = registration.agents["malformed"]

    class SendsGarbage:
        async def run_stream(self, agent_id: str, run_input: dict):
            yield {"type": "RUN_STARTED", "threadId": run_input.thread_id, "runId": run_input.run_id}
            yield {"type": "TEXT_MESSAGE_START", "role": "assistant"}
            yield {"type": "RUN_FINISHED", "threadId": run_input.thread_id, "runId": run_input.run_id}

    await brisk.attach(identity, SendsGarbage(), [agent_id.name])
    handle = await brisk.start_run(agent_id, {"messages": []})

    events = [e async for e in handle.events()]

    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1]["message"] == "provider sent a malformed AG-UI event"

    await _until(lambda: handle.run_id not in brisk.active_runs())
    async with brisk.session() as session:
        stored = await repo.get_run(session, handle.run_id)
        persisted = await repo.get_run_events(session, handle.run_id)
    assert stored.status == "failed"
    assert stored.metadata["failureReason"] == "provider sent a malformed AG-UI event"
    assert [e["type"] for e in persisted] == ["RUN_STARTED", "RUN_ERROR"]


async def test_a_provider_that_reports_its_own_failure_is_not_corrected(brisk):
    registration, identity = await _register(brisk, "polite")
    agent_id = registration.agents["polite"]

    class ReportsItsOwn:
        async def run_stream(self, agent_id: str, run_input: dict):
            yield {"type": "RUN_STARTED", "threadId": run_input.thread_id, "runId": run_input.run_id}
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
            yield {"type": "RUN_STARTED", "threadId": run_input.thread_id, "runId": run_input.run_id}
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
    funduq = Funduq(settings, broker=RunBroker(unserved_timeout_seconds=0.05))
    await funduq.start()
    try:
        _registration, identity = await _register(funduq, "unserved")
        agent = _registration.agents["unserved"]

        handle = await funduq.start_run(agent, {"messages": []})
        async with asyncio.timeout(5):
            [_ async for _ in handle.events()]

        await _until(lambda: handle.run_id not in funduq.active_runs())
        run = await funduq.get_run(handle.run_id)
        assert run.status == "failed"
        assert run.metadata["failureReason"] == "no_provider_took_it"
    finally:
        await funduq.aclose()


async def test_an_event_type_funduq_does_not_know_is_relayed_untouched(brisk):
    registration, identity = await _register(brisk, "futuristic")
    agent_id = registration.agents["futuristic"]
    future_event = {
        "type": "SOME_FUTURE_EVENT",
        "payload": {"nested": ["anything", 42]},
        "rawEvent": None,
    }

    class SpeaksNewerAgUi:
        async def run_stream(self, agent_id: str, run_input: dict):
            ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
            yield {"type": "RUN_STARTED", **ids}
            yield dict(future_event)
            yield {"type": "RUN_FINISHED", **ids}

    await brisk.attach(identity, SpeaksNewerAgUi(), [agent_id.name])
    handle = await brisk.start_run(agent_id, {"messages": []})

    events = [e async for e in handle.events()]

    assert [e["type"] for e in events] == ["RUN_STARTED", "SOME_FUTURE_EVENT", "RUN_FINISHED"]
    assert events[1] == future_event, "the relay must not rewrite what it does not understand"

    await _until(lambda: handle.run_id not in brisk.active_runs())
    async with brisk.session() as session:
        stored = await repo.get_run(session, handle.run_id)
        persisted = await repo.get_run_events(session, handle.run_id)
    assert stored.status == "completed"
    assert [e["type"] for e in persisted] == ["RUN_STARTED", "SOME_FUTURE_EVENT", "RUN_FINISHED"]
    assert persisted[1] == future_event


async def test_an_event_with_no_type_string_is_malformation_not_version_skew(brisk):
    registration, identity = await _register(brisk, "typeless")
    agent_id = registration.agents["typeless"]

    class SendsTypeless:
        async def run_stream(self, agent_id: str, run_input: dict):
            ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
            yield {"type": "RUN_STARTED", **ids}
            yield {"payload": "no type at all"}
            yield {"type": "RUN_FINISHED", **ids}

    await brisk.attach(identity, SendsTypeless(), [agent_id.name])
    handle = await brisk.start_run(agent_id, {"messages": []})

    events = [e async for e in handle.events()]

    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1]["message"] == "provider sent a malformed AG-UI event"
