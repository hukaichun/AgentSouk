from __future__ import annotations

import asyncio

from souk.broker import RelayEvent, RunBroker
from souk.models import AgentRef

AGENT = AgentRef(provider_key="sdk_1", name="agent_1")


class _Taker:

    public_key = "sdk_1"
    max_concurrent_runs = None

    async def deliver(self, run) -> bool:
        return True

    def cancel(self, run_id: str) -> None:
        pass


async def _delivered(broker: RunBroker, key: str = "sdk_1"):
    provider = _Taker()
    provider.public_key = key
    broker.register_provider({AGENT: provider})
    run = broker.enqueue_run("run_1", AGENT, "thread_1", {}, "ag-ui")
    async with asyncio.timeout(1):
        while run.claimed_by is None:
            await asyncio.sleep(0)
    run.in_queue.get_nowait()
    return run


async def test_a_reported_event_lands_on_the_runs_own_queue_untouched(souk):
    broker = RunBroker()
    broker.start()
    souk.broker, original = broker, souk.broker
    try:
        run = await _delivered(broker)

        event = {"type": "CUSTOM", "value": object()}
        assert souk.report_event("run_1", event, claimed_by="sdk_1") is True

        assert run.in_queue.qsize() == 1
        queued = run.in_queue.get_nowait()
        assert isinstance(queued, RelayEvent)
        assert queued.event is event
    finally:
        broker.stop()
        souk.broker = original


async def test_an_event_for_someone_elses_run_is_refused(souk):
    broker = RunBroker()
    broker.start()
    souk.broker, original = broker, souk.broker
    try:
        run = await _delivered(broker, key="sdk_owner")

        assert souk.report_event("run_1", {"type": "CUSTOM"}, claimed_by="sdk_impostor") is False
        assert souk.finish_run("run_1", claimed_by="sdk_impostor") is False
        assert run.in_queue.qsize() == 0

        assert souk.report_event("run_1", {"type": "CUSTOM"}, claimed_by="sdk_owner") is True
    finally:
        broker.stop()
        souk.broker = original


def test_reporting_for_a_run_souk_no_longer_has_is_not_an_error(souk):
    assert souk.report_event("run_gone", {"type": "CUSTOM"}, claimed_by="sdk_1") is False
    assert souk.finish_run("run_gone", claimed_by="sdk_1") is False
