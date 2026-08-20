from __future__ import annotations

from funduq.pause import interrupt_outcome_of


def test_interrupt_outcome_of_extracts_interrupts_from_a_run_finished_event():
    event = {
        "type": "RUN_FINISHED",
        "outcome": {"type": "interrupt", "interrupts": [{"id": "int_1", "reason": "tool_call"}]},
    }
    assert interrupt_outcome_of(event) == [{"id": "int_1", "reason": "tool_call"}]


def test_interrupt_outcome_of_is_none_for_a_plain_success_finish():
    assert interrupt_outcome_of({"type": "RUN_FINISHED", "outcome": {"type": "success"}}) is None
    assert interrupt_outcome_of({"type": "RUN_FINISHED"}) is None


def test_interrupt_outcome_of_is_none_for_non_run_finished_events():
    assert interrupt_outcome_of({"type": "TEXT_MESSAGE_START"}) is None
    assert interrupt_outcome_of({"type": "CUSTOM", "name": "anything"}) is None
