from __future__ import annotations

import asyncio

import pytest

from funduq.broker import Claim, Fail, FinishStream, RelayEvent, RequestCancel, RunBroker
from funduq.models import AgentRef

AGENT = AgentRef(provider_key="pk_1", name="agent_1")


class Taker:

    public_key = "pk_1"
    max_concurrent_runs = None

    def __init__(self) -> None:
        self.asked_to_stop: list[str] = []

    async def deliver(self, run) -> bool:
        return True

    def cancel(self, run_id: str) -> None:
        self.asked_to_stop.append(run_id)


@pytest.fixture
async def broker():
    b = RunBroker()
    b.start()
    try:
        yield b
    finally:
        b.stop()


async def _until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0)


async def _delivered(broker: RunBroker, handlers: dict, run_id: str = "run_1"):
    broker.register_provider({AGENT: Taker()})
    run = broker.enqueue_run(run_id, AGENT, "thread_1", {}, "ag-ui", handlers)
    await _until(lambda: run.claimed_by is not None)
    return run


async def test_next_seq_increments_for_a_known_run(broker):
    run = broker.enqueue_run("run_1", AGENT, "thread_1", {}, "ag-ui")
    run.seq += 1
    run.seq += 1
    assert run.seq == 2


async def test_the_pipeline_dispatches_commands_to_the_right_handler_in_order(broker):
    calls: list[str] = []

    async def record(name):
        async def handler(run, cmd):
            calls.append(name)

        return handler

    handlers = {
        Claim: await record("claim"),
        RelayEvent: await record("relay"),
        FinishStream: await record("finish"),
    }
    run = await _delivered(broker, handlers)
    run.in_queue.put_nowait(RelayEvent({}))
    run.in_queue.put_nowait(FinishStream())

    await _until(lambda: calls == ["claim", "relay", "finish"])


async def test_the_pipeline_forgets_the_run_once_finish_stream_is_processed(broker):
    async def on_finish(run, cmd):
        pass

    run = await _delivered(broker, {FinishStream: on_finish})
    run.in_queue.put_nowait(FinishStream())

    await _until(lambda: broker.get("run_1") is None)


async def test_the_pipeline_stays_alive_after_a_cancel_and_waits_for_the_finish(broker):
    seen: list[str] = []

    async def on_claim(run, cmd):
        pass

    async def on_cancel(run, cmd):
        seen.append("cancel")

    async def on_finish(run, cmd):
        seen.append("finish")

    handlers = {Claim: on_claim, RequestCancel: on_cancel, FinishStream: on_finish}
    run = await _delivered(broker, handlers)
    run.in_queue.put_nowait(RequestCancel())

    await _until(lambda: seen == ["cancel"])
    assert broker.get("run_1") is not None

    run.in_queue.put_nowait(FinishStream())
    await _until(lambda: broker.get("run_1") is None)
    assert seen == ["cancel", "finish"]


async def test_the_pipeline_forgets_the_run_when_the_health_sweep_gives_up_on_it(broker):
    async def on_fail(run, cmd):
        pass

    run = await _delivered(broker, {Fail: on_fail})
    run.in_queue.put_nowait(Fail("stalled"))

    await _until(lambda: broker.get("run_1") is None)


async def test_cancelling_a_queued_run_records_it_once_and_ends_it(broker):
    seen: list[str] = []

    async def on_cancel(run, cmd):
        seen.append("cancel")

    run = broker.enqueue_run("run_1", AGENT, "thread_1", {}, "ag-ui", {RequestCancel: on_cancel})
    assert run.claimed_by is None

    broker.request_cancel("run_1")

    await _until(lambda: broker.get("run_1") is None)
    assert seen == ["cancel"]


async def test_request_cancel_marks_the_run_before_anything_else_happens(broker):
    run = broker.enqueue_run("run_1", AGENT, "thread_1", {}, "ag-ui")

    assert not run.cancel_requested
    broker.request_cancel("run_1")
    assert run.cancel_requested
