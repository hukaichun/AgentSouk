"""How far an event travels between the worker and the caller.

The pull model made this longer than it needed to be: an event arriving on a
wire was routed into a per-run queue inside the transport, drained by a pump
task turning that push back into the pull the provider port described, and
only then pushed onto the run's own queue. Two queues and two routing tables
before it reached the pipeline that persists it.

What this file pins is the shape that replaced it — one lookup, in core's
own run registry, and nothing per run anywhere else:

    event ─→ Souk.report_event ─→ run.in_queue ─→ (persist) ─→ run.out_queue

so it is a regression test for the layering rather than for a behaviour: the
suite would stay green if a transport started keeping a queue per run again.
"""

from __future__ import annotations

from souk.broker import RelayEvent, RunBroker



def test_a_reported_event_lands_on_the_runs_own_queue_untouched(souk):
    """One hop, and the same object. Anything that re-queued or re-wrapped
    the event on the way in would show up here as a different object."""
    broker = RunBroker()
    souk.broker, original = broker, souk.broker
    try:
        # handlers=None: nothing consumes the queue, so what arrives on it
        # is observable rather than instantly drained.
        run = broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui")
        broker.claim(["agent_1"], claimed_by="sdk_1")
        run.in_queue.get_nowait()  # the Claim

        event = {"type": "CUSTOM", "value": object()}
        assert souk.report_event("run_1", event, claimed_by="sdk_1") is True

        assert run.in_queue.qsize() == 1
        queued = run.in_queue.get_nowait()
        assert isinstance(queued, RelayEvent)
        assert queued.event is event
    finally:
        souk.broker = original


def test_an_event_for_someone_elses_run_is_refused(souk):
    """Being connected is not the same as holding the run. Without this,
    any authenticated provider could push events into any run_id it could
    guess — including another provider's."""
    broker = RunBroker()
    souk.broker, original = broker, souk.broker
    try:
        run = broker.enqueue_run("run_1", "agent_1", "thread_1", {}, "ag-ui")
        broker.claim(["agent_1"], claimed_by="sdk_owner")
        run.in_queue.get_nowait()  # the Claim

        assert souk.report_event("run_1", {"type": "CUSTOM"}, claimed_by="sdk_impostor") is False
        assert souk.finish_run("run_1", claimed_by="sdk_impostor") is False
        assert run.in_queue.qsize() == 0

        # And nothing about it stops the rightful holder.
        assert souk.report_event("run_1", {"type": "CUSTOM"}, claimed_by="sdk_owner") is True
    finally:
        souk.broker = original


def test_reporting_for_a_run_souk_no_longer_has_is_not_an_error(souk):
    """A straggler from a run the health sweep already gave up on, or one
    that finished as the frame was in flight. Ordinary, not a fault."""
    assert souk.report_event("run_gone", {"type": "CUSTOM"}, claimed_by="sdk_1") is False
    assert souk.finish_run("run_gone", claimed_by="sdk_1") is False
