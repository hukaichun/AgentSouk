from __future__ import annotations

import asyncio
import logging

import pytest

from funduq.broker import Fail, RequestCancel, RunBroker
from funduq.models import AgentRef

AGENT = AgentRef(provider_key="pk_provider", name="translator")
OTHER = AgentRef(provider_key="pk_provider", name="summarizer")


async def _until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


class Recording:

    def __init__(
        self,
        key: str = "pk_provider",
        *,
        max_concurrent_runs: int | None = None,
        answers: list[bool] | None = None,
        default: bool = True,
        hang: bool = False,
    ) -> None:
        self.public_key = key
        self.max_concurrent_runs = max_concurrent_runs
        self._answers = list(answers or [])
        self._default = default
        self._hang = hang
        self.offered: list[str] = []
        self.cancelled: list[str] = []

    async def deliver(self, run) -> bool:
        self.offered.append(run.run_id)
        if self._hang:
            await asyncio.Event().wait()
        return self._answers.pop(0) if self._answers else self._default

    def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)


@pytest.fixture
async def broker():
    b = RunBroker(deliver_timeout_seconds=0.05, unserved_timeout_seconds=30)
    b.start()
    try:
        yield b
    finally:
        b.stop()


def _enqueue(broker: RunBroker, run_id: str, agent: AgentRef = AGENT, thread_id: str | None = None):
    # Each run gets its own thread unless a test is about thread order:
    # dispatch is one turn per thread at a time, and these tests exercise
    # delivery and capacity, not the thread gate.
    return broker.enqueue_run(
        run_id, agent, thread_id or f"thread_{run_id}", {"messages": []}, "ag-ui", {}
    )


async def test_an_ack_starts_the_run_and_takes_a_place(broker):
    provider = Recording()
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")

    await _until(lambda: provider.offered == ["run_1"])
    await _until(lambda: broker.get("run_1").claimed_by == "pk_provider")

    assert broker.quality()["pk_provider"].in_flight == 1


async def test_the_provider_is_handed_a_value_not_funduqes_dispatch_state(broker):
    handed: list = []

    class Inspecting(Recording):
        async def deliver(self, run) -> bool:
            handed.append(run)
            return await super().deliver(run)

    broker.register_provider({AGENT: Inspecting()})
    _enqueue(broker, "run_1")
    await _until(lambda: bool(handed))

    run = handed[0]
    assert (run.run_id, run.agent, run.run_input) == ("run_1", AGENT, {"messages": []})
    assert not hasattr(run, "in_queue") and not hasattr(run, "out_queue")


async def test_a_run_is_delivered_exactly_once(broker):
    provider = Recording()
    broker.register_provider({AGENT: provider})
    for i in range(5):
        _enqueue(broker, f"run_{i}")

    await _until(lambda: len(provider.offered) >= 5)
    await asyncio.sleep(0.05)

    assert sorted(provider.offered) == [f"run_{i}" for i in range(5)]


async def test_a_decline_leaves_the_run_queued(broker):
    provider = Recording(answers=[False, False], default=False)
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")

    await _until(lambda: bool(provider.offered))
    await asyncio.sleep(0.05)

    assert broker.get("run_1").claimed_by is None
    assert broker.quality()["pk_provider"].in_flight == 0


async def test_a_declined_run_is_offered_again_once_something_changes(broker):
    provider = Recording(answers=[False], default=True)
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")
    await _until(lambda: provider.offered == ["run_1"])

    _enqueue(broker, "run_2")

    await _until(lambda: broker.get("run_1").claimed_by == "pk_provider")


async def test_declining_while_funduq_believed_there_was_room_is_recorded(broker):
    provider = Recording(max_concurrent_runs=2, default=False)
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")

    await _until(lambda: bool(provider.offered))
    await _until(lambda: broker.quality()["pk_provider"].misdeclared == 1)

    quality = broker.quality()["pk_provider"]
    assert quality.declared == 2
    assert quality.in_flight == 2


async def test_a_provider_that_never_answers_is_not_waited_on_forever(broker):
    silent = Recording(key="pk_silent", hang=True)
    broker.register_provider({AGENT: silent})
    _enqueue(broker, "run_1")

    await _until(lambda: broker.quality()["pk_silent"].unanswered >= 1, timeout=2.0)
    assert broker.get("run_1").claimed_by is None

    working = Recording(key="pk_working")
    broker.register_provider({OTHER: working})
    _enqueue(broker, "run_2", OTHER)
    await _until(lambda: working.offered == ["run_2"], timeout=2.0)


async def test_a_full_provider_is_offered_nothing(broker):
    provider = Recording(max_concurrent_runs=1)
    broker.register_provider({AGENT: provider, OTHER: provider})
    _enqueue(broker, "run_1", AGENT)
    _enqueue(broker, "run_2", OTHER)

    await _until(lambda: len(provider.offered) == 1)
    await asyncio.sleep(0.05)

    assert len(provider.offered) == 1, "offered past a full provider's declared capacity"


async def test_the_place_comes_back_when_the_run_ends(broker):
    provider = Recording(max_concurrent_runs=1)
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")
    _enqueue(broker, "run_2")
    await _until(lambda: provider.offered == ["run_1"])

    broker.forget("run_1")

    await _until(lambda: provider.offered == ["run_1", "run_2"])
    assert broker.quality()["pk_provider"].in_flight == 1


async def test_a_reconnecting_provider_keeps_its_bucket(broker):
    provider = Recording(max_concurrent_runs=1)
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")
    await _until(lambda: broker.quality()["pk_provider"].in_flight == 1)

    broker.register_provider({AGENT: provider})

    assert broker.quality()["pk_provider"].in_flight == 1


async def test_a_late_ack_is_accepted_from_the_provider_that_owns_the_agent(broker):
    silent = Recording(hang=True)
    broker.register_provider({AGENT: silent})
    _enqueue(broker, "run_1")
    await _until(lambda: broker.quality()["pk_provider"].unanswered >= 1, timeout=2.0)

    assert broker.accept_late_ack("run_1", "pk_provider") is True

    quality = broker.quality()["pk_provider"]
    assert (quality.answered_late, quality.in_flight) == (1, 1)
    assert broker.get("run_1").claimed_by == "pk_provider"


async def test_a_late_ack_from_anyone_else_is_refused(broker):
    silent = Recording(hang=True)
    broker.register_provider({AGENT: silent})
    _enqueue(broker, "run_1")
    await _until(lambda: broker.quality()["pk_provider"].unanswered >= 1, timeout=2.0)

    assert broker.accept_late_ack("run_1", "pk_impostor") is False
    assert broker.get("run_1").claimed_by is None


async def test_taking_a_run_and_never_ending_it_is_recorded(broker):
    provider = Recording()
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")
    await _until(lambda: broker.get("run_1").claimed_by == "pk_provider")

    broker.push("run_1", Fail("stalled"))

    assert broker.quality()["pk_provider"].abandoned == 1


async def test_a_run_nobody_ever_takes_is_given_up_on(broker):
    _enqueue(broker, "run_1")

    expired = broker.expire_queued(timeout_seconds=0)

    assert expired == ["run_1"]
    await _until(lambda: broker.get("run_1") is None)


async def test_cancelling_a_delivered_run_keeps_the_provider_in_the_loop(broker):
    provider = Recording()
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")
    await _until(lambda: broker.get("run_1").claimed_by == "pk_provider")

    assert broker.request_cancel("run_1") is True

    snapshot = broker.get("run_1")
    assert snapshot.cancel_requested is True
    assert snapshot.claimed_by == "pk_provider"


async def test_cancelling_a_queued_run_stops_it_ever_being_offered(broker):
    provider = Recording()
    _enqueue(broker, "run_1")

    assert broker.request_cancel("run_1") is True
    broker.register_provider({AGENT: provider})
    await asyncio.sleep(0.05)

    assert provider.offered == []


async def test_a_cancelled_run_does_not_block_the_one_behind_it(broker):
    provider = Recording()
    _enqueue(broker, "run_1")
    _enqueue(broker, "run_2")
    broker.request_cancel("run_1")
    broker.register_provider({AGENT: provider})

    await _until(lambda: provider.offered == ["run_2"])


async def test_the_place_comes_back_when_a_delivered_run_is_cancelled(broker):
    provider = Recording(max_concurrent_runs=1)
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1")
    _enqueue(broker, "run_2")
    await _until(lambda: provider.offered == ["run_1"])

    broker.request_cancel("run_1")
    broker.push("run_1", Fail("cancelled"))
    broker.forget("run_1")

    await _until(lambda: provider.offered == ["run_1", "run_2"])


def test_a_broker_can_be_built_outside_a_loop_and_started_in_two(caplog):
    broker = RunBroker(deliver_timeout_seconds=0.05)

    async def place_one(run_id: str) -> list[str]:
        provider = Recording()
        broker.start()
        try:
            broker.register_provider({AGENT: provider})
            _enqueue(broker, run_id)
            await _until(lambda: provider.offered == [run_id])
            await asyncio.sleep(0.05)
            return provider.offered
        finally:
            broker.forget(run_id)
            broker.stop()

    with caplog.at_level(logging.ERROR, logger="funduq.broker"):
        assert asyncio.run(place_one("run_1")) == ["run_1"]
        assert asyncio.run(place_one("run_2")) == ["run_2"]

    assert [r.getMessage() for r in caplog.records] == []


async def test_a_queued_run_waits_as_long_as_its_agent_is_served():
    b = RunBroker(deliver_timeout_seconds=0.02, unserved_timeout_seconds=0.05)
    b.start()
    try:
        b.register_provider({AGENT: Recording(default=False)})
        _enqueue(b, "run_1")
        await asyncio.sleep(0.25)
        run = b.get("run_1")
        assert run is not None and run.claimed_by is None
    finally:
        b.stop()


async def test_losing_the_provider_starts_the_clock_that_fails_the_run():
    b = RunBroker(deliver_timeout_seconds=0.02, unserved_timeout_seconds=0.05)
    b.start()
    try:
        b.register_provider({AGENT: Recording(default=False)})
        _enqueue(b, "run_1")
        await asyncio.sleep(0.25)
        assert b.get("run_1") is not None

        b.unregister_provider([AGENT])
        await _until(lambda: b.get("run_1") is None, timeout=2.0)
    finally:
        b.stop()


async def test_a_provider_returning_within_the_window_keeps_the_run():
    b = RunBroker(deliver_timeout_seconds=0.02, unserved_timeout_seconds=0.3)
    b.start()
    try:
        b.register_provider({AGENT: Recording(default=False)})
        _enqueue(b, "run_1")
        b.unregister_provider([AGENT])
        await asyncio.sleep(0.05)
        b.register_provider({AGENT: Recording(default=False)})
        await asyncio.sleep(0.6)
        assert b.get("run_1") is not None
    finally:
        b.stop()


async def test_a_threads_runs_are_offered_in_arrival_order_without_waiting(broker):
    """funduq imposes no turn-taking: a thread's second utterance is offered
    while the first is still in flight — whether to run, hold, or absorb it
    is the provider's decision, not the relay's."""
    provider = Recording()
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1", thread_id="thread_shared")
    _enqueue(broker, "run_2", thread_id="thread_shared")

    await _until(lambda: provider.offered == ["run_1", "run_2"])
    assert broker.get("run_1").is_claimed and broker.get("run_2").is_claimed


async def test_a_declined_head_is_not_overtaken_by_its_sibling():
    """Arrival order is the one sequencing funduq does own: while the head of
    the queue stands declined, a later utterance of the same thread must not
    reach the provider first. (A decline is re-offered on the next sweep
    wakeup, hence the short unserved timeout here.)"""
    b = RunBroker(deliver_timeout_seconds=0.05, unserved_timeout_seconds=0.1)
    b.start()
    try:
        provider = Recording(answers=[True, False, False, True, True])
        b.register_provider({AGENT: provider})
        _enqueue(b, "run_1", thread_id="thread_shared")
        _enqueue(b, "run_2", thread_id="thread_shared")
        _enqueue(b, "run_3", thread_id="thread_shared")

        await _until(lambda: "run_3" in provider.offered, timeout=3.0)
        first_run_3 = provider.offered.index("run_3")
        assert provider.offered.index("run_2") < first_run_3, "run_3 overtook run_2"
        assert provider.offered[:first_run_3].count("run_2") >= 3, (
            "run_3 was offered before its declined sibling was finally accepted"
        )
    finally:
        b.stop()


async def test_a_paused_run_does_not_hold_back_its_siblings(broker):
    """A pause is between the provider and the asker; the thread's next
    utterance still reaches the provider, which knows its own paused run and
    decides how to sequence."""
    from funduq.broker import FinishStream

    provider = Recording()
    broker.register_provider({AGENT: provider})
    _enqueue(broker, "run_1", thread_id="t-paused")
    await _until(lambda: provider.offered == ["run_1"])
    broker._runs["run_1"].pause_payload = {"interrupts": []}
    broker.push("run_1", FinishStream())
    await _until(lambda: broker.get("run_1") is None, timeout=2.0)

    _enqueue(broker, "run_2", thread_id="t-paused")
    await _until(lambda: provider.offered == ["run_1", "run_2"], timeout=2.0)
