"""Pure-function unit tests for souk.protocols.a2a_translate — no DB, no HTTP.

Two jobs. One is the RUN_ERROR -> failed mapping the offline-handling sweep
(A7b, souk.health) relies on to give live SSE subscribers an explicit
terminal event instead of the stream just closing.

The other is pinning the *exact* wire shapes, field by field. souk has no A2A
library — it depends on `ag-ui-protocol` for AG-UI and hand-writes A2A — so a
spec rename lands as nothing at all until a client fails against it, which is
how souk went on emitting the original `{"type": "text"}` / `{"id": ...}`
spelling long after the published schema moved to `kind` / `taskId`. These
assertions are the substitute for a library: the shapes below were read off
`a2a-sdk`'s models, and comparing whole dicts (rather than picking at a key
or two) is deliberate, so an extra or renamed field fails here rather than at
a caller.
"""

from __future__ import annotations

from souk.protocols.a2a_translate import (
    agui_event_to_a2a_update,
    a2a_message_to_agui_messages,
    build_task,
    status_update_for_run_status,
)


def test_run_error_event_maps_to_failed_status_with_message():
    update = agui_event_to_a2a_update(
        {"type": "RUN_ERROR", "message": "no_provider_online"}, "task_1", "session_1"
    )
    assert update["status"]["state"] == "failed"
    assert update["final"] is True
    # TaskStatus.message is a Message in the schema, not a string — an AG-UI
    # RUN_ERROR carries the string, so it is wrapped rather than put in a
    # field it doesn't fit.
    assert update["status"]["message"] == {
        "kind": "message",
        "messageId": "task_1-error",
        "role": "agent",
        "parts": [{"kind": "text", "text": "no_provider_online"}],
    }


def test_run_finished_is_final_completed():
    update = agui_event_to_a2a_update({"type": "RUN_FINISHED"}, "task_1", "session_1")
    assert update == {
        "kind": "status-update",
        "taskId": "task_1",
        "contextId": "session_1",
        "status": {"state": "completed"},
        "final": True,
    }


def test_text_content_becomes_an_appending_artifact_update():
    """Keyed by AG-UI's messageId so the deltas of one assistant message are
    one artifact, and `append` says so."""
    update = agui_event_to_a2a_update(
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "hel"}, "task_1", "session_1"
    )
    assert update == {
        "kind": "artifact-update",
        "taskId": "task_1",
        "contextId": "session_1",
        "artifact": {"artifactId": "m1", "parts": [{"kind": "text", "text": "hel"}]},
        "append": True,
    }


def test_unmodeled_event_falls_back_to_nonfinal_working_update():
    event = {"type": "CUSTOM", "name": "sub_agent_progress", "value": {"sub_agent": "translator"}}
    update = agui_event_to_a2a_update(event, "task_1", "session_1")
    assert update["status"]["state"] == "working"
    assert update["final"] is False
    assert update["status"]["metadata"]["agui_event"] == event


def test_status_update_for_run_status_marks_terminal_states_final():
    assert status_update_for_run_status("t1", "s1", "queued")["final"] is False
    assert status_update_for_run_status("t1", "s1", "running")["final"] is False
    assert status_update_for_run_status("t1", "s1", "input-required")["final"] is False
    assert status_update_for_run_status("t1", "s1", "completed")["final"] is True
    assert status_update_for_run_status("t1", "s1", "failed")["final"] is True
    assert status_update_for_run_status("t1", "s1", "cancelled")["final"] is True
    assert status_update_for_run_status("t1", "s1", "cancelled")["status"]["state"] == "canceled"


def test_inbound_parts_are_read_under_either_spelling():
    """Lenient inbound on purpose: `kind` is current, `type` is what souk's
    own older clients send, and rejecting them would buy nothing."""
    current = a2a_message_to_agui_messages(
        {"role": "user", "parts": [{"kind": "text", "text": "hi"}]}
    )
    original = a2a_message_to_agui_messages(
        {"role": "user", "parts": [{"type": "text", "text": "hi"}]}
    )
    assert current == original == [{"role": "user", "content": "hi"}]


def test_build_task_merges_a_message_into_one_artifact():
    """A finished Task carries the text whole — one artifact per assistant
    message, and its deltas joined into one part. It used to carry one
    artifact per streamed token, which is a faithful transcript of the
    stream and a useless answer to `tasks/get`."""
    events = [
        {"type": "RUN_STARTED"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Hello "},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "world"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m2", "delta": "and again"},
        {"type": "RUN_FINISHED"},
    ]

    task = build_task("task_1", "session_1", "translator", "completed", events)

    assert task == {
        "kind": "task",
        "id": "task_1",
        "contextId": "session_1",
        "status": {"state": "completed"},
        "artifacts": [
            {"artifactId": "m1", "parts": [{"kind": "text", "text": "Hello world"}]},
            {"artifactId": "m2", "parts": [{"kind": "text", "text": "and again"}]},
        ],
    }
