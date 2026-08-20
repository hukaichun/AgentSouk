from __future__ import annotations

import asyncio

import pytest

from souk_provider_sdk import InProcessLink, AgentHandle, HandleProvider, ProviderIdentity, ProviderRuntime

from souk import repo
from souk.models import AgentRef


async def _until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
async def runtimes():
    started: list[ProviderRuntime] = []
    yield started
    for runtime in started:
        await runtime.aclose(cancel_in_flight=True)


async def _attach(souk, runtimes, agents: dict, **kwargs) -> ProviderIdentity:
    identity = ProviderIdentity.generate()
    signature, timestamp = identity.sign_registration(list(agents))
    await souk.register_agents(
        identity.public_key, signature, timestamp, [{"name": n} for n in agents]
    )
    runtime = ProviderRuntime(
        identity,
        HandleProvider([AgentHandle(name, fn) for name, fn in agents.items()]),
        **kwargs,
    )
    runtimes.append(runtime)
    runtime.start()
    await souk.attach_provider(InProcessLink(souk, runtime), list(agents))
    return identity


async def test_a_run_goes_all_the_way_out_and_all_the_way_back(souk, runtimes):
    seen: dict = {}

    async def agent(run_input):
        seen["input"] = run_input
        ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_STARTED", **ids}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "bonjour"}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", **ids}

    identity = await _attach(souk, runtimes, {"translator": agent})
    agent_ref = AgentRef(provider_key=identity.public_key, name="translator")

    assert [a.online for a in await souk.list_agents() if a.name == "translator"] == [True]

    handle = await souk.start_run(agent_ref, {"messages": []})
    assert [e["type"] async for e in handle.events()] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    assert seen["input"].run_id == handle.run_id

    await _until(lambda: handle.run_id not in souk.active_runs())
    async with souk.session() as session:
        assert (await repo.get_run(session, handle.run_id)).status == "completed"
        messages = await repo.get_thread_messages(session, handle.thread_id)
    assert messages[-1]["content"] == "bonjour"


async def test_souk_asks_the_provider_that_took_the_run_to_stop(souk, runtimes):
    started = asyncio.Event()

    async def agent(run_input):
        yield {"type": "RUN_STARTED", "threadId": run_input.thread_id, "runId": run_input.run_id}
        started.set()
        await asyncio.sleep(30)

    identity = await _attach(souk, runtimes, {"slow": agent})
    handle = await souk.start_run(
        AgentRef(provider_key=identity.public_key, name="slow"), {"messages": []}
    )
    async with asyncio.timeout(5):
        await started.wait()

    handle.cancel()
    [_ async for _ in handle.events()]

    await _until(lambda: handle.run_id not in souk.active_runs())
    async with souk.session() as session:
        assert (await repo.get_run(session, handle.run_id)).status == "cancelled"


async def test_one_provider_serves_several_agents_on_one_budget(souk, runtimes):
    in_flight = 0
    high_water = 0
    release = asyncio.Event()

    def make(reply: str):
        async def agent(run_input):
            nonlocal in_flight, high_water
            in_flight += 1
            high_water = max(high_water, in_flight)
            ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
            yield {"type": "RUN_STARTED", **ids}
            await release.wait()
            yield {"type": "TEXT_MESSAGE_START", "messageId": "m", "role": "assistant"}
            yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m", "delta": reply}
            yield {"type": "TEXT_MESSAGE_END", "messageId": "m"}
            yield {"type": "RUN_FINISHED", **ids}
            in_flight -= 1

        return agent

    identity = await _attach(
        souk,
        runtimes,
        {"translator": make("translated"), "summarizer": make("summarized")},
        max_concurrent_runs=1,
    )
    handles = [
        await souk.start_run(
            AgentRef(provider_key=identity.public_key, name=name), {"messages": []}
        )
        for name in ("translator", "summarizer")
    ]

    await _until(lambda: in_flight == 1)
    release.set()
    for handle in handles:
        assert [e["type"] async for e in handle.events()][-1] == "RUN_FINISHED"

    assert high_water == 1


async def test_a_provider_that_declares_no_limit_starts_everything_it_is_given(souk, runtimes):
    running = 0
    release = asyncio.Event()

    async def agent(run_input):
        nonlocal running
        running += 1
        try:
            yield {"type": "RUN_STARTED", "threadId": run_input.thread_id, "runId": run_input.run_id}
            await release.wait()
        finally:
            running -= 1

    identity = await _attach(souk, runtimes, {"parallel": agent})
    agent_ref = AgentRef(provider_key=identity.public_key, name="parallel")
    for _ in range(5):
        await souk.start_run(agent_ref, {"messages": []})

    await _until(lambda: running == 5)
    release.set()
