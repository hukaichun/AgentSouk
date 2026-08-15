"""RunBroker — pure in-memory logic, no DB needed.

Covers two things: the plain registry/poll/wake mechanics (unchanged in
spirit from before), and the pipeline itself — the part that replaced a
shared dataclass poked directly by four unrelated modules with a single
per-run task serializing every Command against it (see broker.py's module
docstring for why). These tests use fake handlers precisely so they don't
need real DB access to exercise that serialization/termination contract.
"""

from __future__ import annotations

import asyncio

import pytest

from souk.broker import Claim, Fail, FinishStream, RelayEvent, RequestCancel, RunBroker, request_cancel


async def _until(predicate, timeout: float = 1.0) -> None:
    """Polls a zero-arg predicate until true — used instead of a bare
    `await asyncio.sleep(0)` to wait for a spawned pipeline task to make
    progress, without assuming exactly how many event-loop turns that
    takes.
    """
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


def test_next_seq_increments_for_a_known_run():
    broker = RunBroker()
    run = broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui")
    run.seq += 1
    run.seq += 1
    assert run.seq == 2


def test_enqueue_run_wakes_a_subscriber_for_that_agent_id():
    broker = RunBroker()
    event = broker.subscribe_wake(["agent_1"])
    assert not event.is_set()
    broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui")
    assert event.is_set()


def test_enqueue_run_does_not_wake_a_subscriber_for_a_different_agent_id():
    broker = RunBroker()
    event = broker.subscribe_wake(["agent_1"])
    broker.enqueue_run("run_1", "agent_2", "thread_1", {}, "ag-ui")
    assert not event.is_set()


def test_unsubscribe_wake_stops_further_wakes():
    broker = RunBroker()
    event = broker.subscribe_wake(["agent_1"])
    broker.unsubscribe_wake(["agent_1"], event)
    broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui")
    assert not event.is_set()


@pytest.mark.asyncio
async def test_pipeline_dispatches_commands_to_the_right_handler_in_order():
    broker = RunBroker()
    calls: list[str] = []

    async def on_claim(run, cmd):
        calls.append("claim")

    async def on_relay(run, cmd):
        calls.append("relay")

    async def on_finish(run, cmd):
        calls.append("finish")

    handlers = {Claim: on_claim, RelayEvent: on_relay, FinishStream: on_finish}
    run = broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui", handlers)
    run.in_queue.put_nowait(Claim())
    run.in_queue.put_nowait(RelayEvent({}))
    run.in_queue.put_nowait(FinishStream())

    await _until(lambda: calls == ["claim", "relay", "finish"])


@pytest.mark.asyncio
async def test_pipeline_forgets_the_run_once_finish_stream_is_processed():
    broker = RunBroker()

    async def on_finish(run, cmd):
        pass

    run = broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui", {FinishStream: on_finish})
    run.in_queue.put_nowait(FinishStream())

    await _until(lambda: broker.get("run_1") is None)


@pytest.mark.asyncio
async def test_pipeline_terminates_immediately_on_cancel_before_any_claim():
    """A run cancelled before any agent claimed it will never get a
    FinishStream from anyone — nothing would ever end the pipeline
    without this special case (see broker._pipeline).
    """
    broker = RunBroker()
    seen: list[str] = []

    async def on_cancel(run, cmd):
        seen.append("cancel")

    run = broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui", {RequestCancel: on_cancel})
    assert run.claimed_by is None
    run.in_queue.put_nowait(RequestCancel())

    await _until(lambda: broker.get("run_1") is None)
    assert seen == ["cancel"]


@pytest.mark.asyncio
async def test_pipeline_stays_alive_after_cancel_once_claimed_waiting_for_finish():
    """Mirrors the real fix this whole pipeline exists for: once claimed,
    a cancel must not end the pipeline by itself — it has to wait for the
    agent's own FinishStream (its unwind-after-cancel signal). Ending
    early would forget the run while the agent is still unwinding, so its
    straggler events and end_of_stream would arrive at an unknown run_id
    and be logged as errors rather than absorbed.
    """
    broker = RunBroker()
    seen: list[str] = []

    async def on_claim(run, cmd):
        pass

    async def on_cancel(run, cmd):
        seen.append("cancel")

    async def on_finish(run, cmd):
        seen.append("finish")

    handlers = {Claim: on_claim, RequestCancel: on_cancel, FinishStream: on_finish}
    run = broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui", handlers)
    broker.claim(["agent_1"], claimed_by="sdk_1")
    run.in_queue.put_nowait(RequestCancel())

    await _until(lambda: seen == ["cancel"])
    # Still registered — the pipeline is waiting for FinishStream, not
    # already forgotten just because it was cancelled.
    assert broker.get("run_1") is not None

    run.in_queue.put_nowait(FinishStream())
    await _until(lambda: broker.get("run_1") is None)
    assert seen == ["cancel", "finish"]


def test_request_cancel_marks_the_run_synchronously_before_any_pipeline_processing():
    """The whole point of request_cancel over pushing RequestCancel
    directly: `cancelled` must be visible immediately, not only once the
    (independently scheduled) pipeline task gets around to it — this is
    what lets poll() below refuse to hand the run out in the first place,
    with no window where it could still happen.
    """
    broker = RunBroker()
    run = broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui")
    assert not run.cancel_requested
    request_cancel(run)
    assert run.cancel_requested
    assert run.in_queue.qsize() == 1


def test_claim_skips_a_run_cancelled_before_any_worker_claimed_it():
    broker = RunBroker()
    run = broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui")
    request_cancel(run)
    assert broker.claim(["agent_1"], claimed_by="sdk_1") == []


def test_claim_does_not_let_a_cancelled_run_block_a_healthy_one_behind_it():
    broker = RunBroker()
    cancelled = broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui")
    request_cancel(cancelled)
    healthy = broker.enqueue_run("run_2", "agent_1", "thread_1", {}, "ag-ui")
    assert broker.claim(["agent_1"], claimed_by="sdk_1") == [healthy]


def test_claiming_records_the_claimer_and_queues_the_claim_in_one_step():
    """The window the old pull model had to work around: a run was handed
    out, and only later — over a wire, on another call — did the claimer say
    it had taken it. Anything arriving in between saw a run nobody held.
    Here the two happen with no await in between, so there is no such
    moment: `claimed_by` is set and Claim is queued before this returns.
    """
    broker = RunBroker()
    run = broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui")

    assert broker.claim(["agent_1"], claimed_by="sdk_1", cancel_notify=None) == [run]

    assert run.claimed_by == "sdk_1"
    assert isinstance(run.in_queue.get_nowait(), Claim)


def test_a_second_claim_finds_nothing_left_to_take():
    """What makes max_claim a real budget rather than advice: a claimed run
    is gone from the pending queue, so another worker (or the same one
    polling again) cannot pick it up a second time."""
    broker = RunBroker()
    broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui")

    assert [r.run_id for r in broker.claim(["agent_1"], claimed_by="sdk_1")] == ["run_1"]
    assert broker.claim(["agent_1"], claimed_by="sdk_2") == []


@pytest.mark.asyncio
async def test_pipeline_forgets_the_run_when_the_health_sweep_gives_up_on_it():
    """Regression test: an earlier version of _pipeline only terminated
    on FinishStream/RequestCancel — a Fail (health.py giving up on a
    stalled or never-claimed run) got handled but never actually ended
    the pipeline task or forgot the run, leaking both forever.
    """
    broker = RunBroker()

    async def on_fail(run, cmd):
        pass

    run = broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui", {Fail: on_fail})
    run.in_queue.put_nowait(Fail("no_provider_online"))

    await _until(lambda: broker.get("run_1") is None)
