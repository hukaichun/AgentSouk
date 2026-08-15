"""A run driven end to end by a local agent, with no socket anywhere — and
paced by the same knob a remote provider has.

The provider here is an ordinary object with the port's one method
(`run_stream(agent_id, run_input)`, see souk/providers.py). souk supplies the
worker around it (souk/worker.py), which is the thing that claims runs and
decides how many to have in flight. That split is what this file is about:
before it, an attached provider was something souk *called*, so it had no say
in how much work it got — five runs enqueued meant five runs started,
measured. A remote provider could always say `max_claim=2`; now an in-process
one can too, and it is the same number reaching the same code.

The worker loop is exercised for real here (attach and let it claim), rather
than by pushing commands onto a queue — pushing a Claim by hand was possible
when a claim carried the provider to call back into, and it is exactly the
shortcut that let in-process work take a different path from remote work.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk import repo
from souk.config import CoreSettings
from souk.core import Souk
from souk.identity import registration_signing_payload


async def _register(souk, *names: str):
    """Registers a fresh provider identity, and hands back both halves a
    caller needs afterwards: what souk issued, and the public key that *is*
    this provider (what it attaches and claims as)."""
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


async def _until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.005)


@pytest.fixture
async def brisk_souk(settings: CoreSettings):
    """A souk whose worker tops up quickly. The default interval is tuned
    for a real deployment (a busy worker re-checks every couple of seconds);
    a test that deliberately runs a worker out of capacity would otherwise
    spend that interval waiting for it to notice."""
    souk = Souk(settings.model_copy(update={"worker_poll_interval_seconds": 0.02}))
    try:
        yield souk
    finally:
        await souk.aclose()


async def test_a_local_agent_can_drive_a_run_with_no_transport(souk):
    """What this buys: an agent that is a plain object, no registration over
    HTTP, no AgentSession, no ports bound — and the same broker, handlers and
    persistence carrying it through as for a remote one."""
    registration, public_key = await _register(souk, "local")
    agent_id = registration.agent_ids["local"]
    seen: dict = {}

    class LocalProvider:
        async def run_stream(self, agent_id: str, run_input: dict):
            seen["input"] = run_input
            yield {"type": "RUN_STARTED", "threadId": run_input["threadId"], "runId": run_input["runId"]}
            yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
            yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "hello from in-process"}
            yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
            yield {"type": "RUN_FINISHED", "threadId": run_input["threadId"], "runId": run_input["runId"]}

    await souk.attach_provider(public_key, LocalProvider(), [agent_id])
    handle = await souk.start_run(agent_id, {"messages": []})

    assert [e["type"] async for e in handle.events()] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_FINISHED",
    ]
    # The agent really was handed the run's input, by the claim itself.
    assert seen["input"]["runId"] == handle.run_id

    await _until(lambda: handle.run_id not in souk.active_runs())
    async with souk.session() as session:
        stored = await repo.get_run(session, handle.run_id)
        assert stored.status == "completed"
        messages = await repo.get_thread_messages(session, handle.thread_id)
    assert messages[-1]["content"] == "hello from in-process"


async def test_max_claim_caps_what_an_in_process_provider_starts_at_once(brisk_souk):
    """The asymmetry this refactor removed. Five runs, capacity two: three
    of them stay queued until something finishes, instead of all five
    starting the moment they are enqueued (which is what an attached
    provider did, measured, because souk pushed rather than it claiming).
    """
    registration, public_key = await _register(brisk_souk, "capped")
    agent_id = registration.agent_ids["capped"]

    running = 0
    high_water = 0
    release = asyncio.Event()

    class SlowProvider:
        async def run_stream(self, agent_id: str, run_input: dict):
            nonlocal running, high_water
            running += 1
            high_water = max(high_water, running)
            try:
                yield {
                    "type": "RUN_STARTED",
                    "threadId": run_input["threadId"],
                    "runId": run_input["runId"],
                }
                await release.wait()
                yield {
                    "type": "RUN_FINISHED",
                    "threadId": run_input["threadId"],
                    "runId": run_input["runId"],
                }
            finally:
                running -= 1

    await brisk_souk.attach_provider(public_key, SlowProvider(), [agent_id], max_claim=2)

    handles = [await brisk_souk.start_run(agent_id, {"messages": []}) for _ in range(5)]
    await _until(lambda: running == 2)
    # Give the worker every chance to over-claim if it were going to.
    await asyncio.sleep(0.2)
    assert high_water == 2

    release.set()
    for handle in handles:
        assert [e["type"] async for e in handle.events()][-1] == "RUN_FINISHED"
    # And the cap throttled rather than dropped: every run still ran.
    assert high_water == 2


async def test_an_unlimited_worker_starts_everything_it_is_given(brisk_souk):
    """The default is still unlimited, matching the remote SDK's — the
    change is that it is now a choice."""
    registration, public_key = await _register(brisk_souk, "uncapped")
    agent_id = registration.agent_ids["uncapped"]

    running = 0
    release = asyncio.Event()

    class SlowProvider:
        async def run_stream(self, agent_id: str, run_input: dict):
            nonlocal running
            running += 1
            try:
                yield {
                    "type": "RUN_STARTED",
                    "threadId": run_input["threadId"],
                    "runId": run_input["runId"],
                }
                await release.wait()
            finally:
                running -= 1

    await brisk_souk.attach_provider(public_key, SlowProvider(), [agent_id])
    for _ in range(5):
        await brisk_souk.start_run(agent_id, {"messages": []})

    await _until(lambda: running == 5)
    release.set()


async def test_one_worker_serves_several_agents_on_one_budget(brisk_souk):
    """One provider object hosting a translator and a summarizer, exactly
    like one SDK process registering a batch — so its capacity is a budget
    across both, not two independent ones."""
    registration, public_key = await _register(brisk_souk, "translator", "summarizer")
    translator = registration.agent_ids["translator"]
    summarizer = registration.agent_ids["summarizer"]

    running = 0
    high_water = 0
    release = asyncio.Event()

    labels = {translator: "translated", summarizer: "summarized"}

    class PairProvider:
        async def run_stream(self, agent_id: str, run_input: dict):
            nonlocal running, high_water
            running += 1
            high_water = max(high_water, running)
            try:
                yield {
                    "type": "RUN_STARTED",
                    "threadId": run_input["threadId"],
                    "runId": run_input["runId"],
                }
                await release.wait()
                yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
                yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": labels[agent_id]}
                yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
                yield {
                    "type": "RUN_FINISHED",
                    "threadId": run_input["threadId"],
                    "runId": run_input["runId"],
                }
            finally:
                running -= 1

    await brisk_souk.attach_provider(
        public_key, PairProvider(), [translator, summarizer], max_claim=1
    )

    first = await brisk_souk.start_run(translator, {"messages": []})
    second = await brisk_souk.start_run(summarizer, {"messages": []})
    await _until(lambda: running == 1)
    await asyncio.sleep(0.2)
    assert high_water == 1  # one budget, shared

    release.set()
    replies = {}
    for agent_id, handle in ((translator, first), (summarizer, second)):
        events = [e async for e in handle.events()]
        replies[agent_id] = next(e["delta"] for e in events if e.get("delta"))
    # Each run reached the agent it was for: the provider is handed the
    # claimed run's agent_id and routes on it.
    assert replies == {translator: "translated", summarizer: "summarized"}
