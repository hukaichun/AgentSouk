"""RunBroker — pure in-memory logic, no DB needed. Covers the race a
straggler event can hit: a frame arriving for a run whose HTTP/SSE
consumer already finished and called forget() (see grpc_server.py's
_relay_event, which now treats next_seq()/deliver_event() returning
"unknown" as drop-the-frame, not crash).
"""

from __future__ import annotations

import pytest

from souk.broker import RunBroker


def test_next_seq_increments_for_a_known_run():
    broker = RunBroker()
    broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui")
    assert broker.next_seq("run_1") == 1
    assert broker.next_seq("run_1") == 2


def test_next_seq_returns_none_for_a_forgotten_run():
    broker = RunBroker()
    broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui")
    broker.forget("run_1")
    assert broker.next_seq("run_1") is None


@pytest.mark.asyncio
async def test_deliver_event_is_a_noop_for_a_forgotten_run():
    broker = RunBroker()
    broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui")
    broker.forget("run_1")
    # Must not raise — a straggler frame for an already-finished run is a
    # legitimate race, not a bug.
    await broker.deliver_event("run_1", {"type": "TEXT_MESSAGE_CONTENT"})


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
