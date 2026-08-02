"""Pure-function unit tests for souk.translate_a2a — no DB, no HTTP.
Covers the RUN_ERROR -> failed mapping the offline-handling sweep (A7b,
souk.health) relies on to give live SSE subscribers an explicit terminal
event instead of the stream just closing.
"""

from __future__ import annotations

from souk.translate_a2a import (
    agui_event_to_a2a_update,
    status_update_for_run_status,
)


def test_run_error_event_maps_to_failed_status_with_message():
    update = agui_event_to_a2a_update(
        {"type": "RUN_ERROR", "message": "no_provider_online"}, "task_1"
    )
    assert update["status"]["state"] == "failed"
    assert update["status"]["message"] == "no_provider_online"
    assert update["final"] is True


def test_run_finished_is_final_completed():
    update = agui_event_to_a2a_update({"type": "RUN_FINISHED"}, "task_1")
    assert update == {"id": "task_1", "status": {"state": "completed"}, "final": True}


def test_unmodeled_event_falls_back_to_nonfinal_working_update():
    event = {"type": "CUSTOM", "name": "sub_agent_progress", "value": {"sub_agent": "translator"}}
    update = agui_event_to_a2a_update(event, "task_1")
    assert update["status"]["state"] == "working"
    assert update["final"] is False
    assert update["status"]["metadata"]["agui_event"] == event


def test_status_update_for_run_status_marks_terminal_states_final():
    assert status_update_for_run_status("t1", "queued")["final"] is False
    assert status_update_for_run_status("t1", "running")["final"] is False
    assert status_update_for_run_status("t1", "input-required")["final"] is False
    assert status_update_for_run_status("t1", "completed")["final"] is True
    assert status_update_for_run_status("t1", "failed")["final"] is True
    assert status_update_for_run_status("t1", "cancelled")["final"] is True
    assert status_update_for_run_status("t1", "cancelled")["status"]["state"] == "canceled"
