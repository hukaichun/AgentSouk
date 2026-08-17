from __future__ import annotations

from a2a.types import a2a_pb2 as pb

from souk.protocols.a2a_translate import (
    a2a_message_to_agui_messages,
    agui_event_to_a2a_update,
    build_task,
    state_for_run_status,
    status_update_for_run_status,
)


def test_run_error_event_maps_to_failed_status_with_message():
    update = agui_event_to_a2a_update(
        {"type": "RUN_ERROR", "message": "no_provider_online"}, "task_1", "session_1"
    )

    assert update == {
        "statusUpdate": {
            "taskId": "task_1",
            "contextId": "session_1",
            "status": {
                "state": "TASK_STATE_FAILED",
                "message": {
                    "messageId": "task_1-error",
                    "role": "ROLE_AGENT",
                    "parts": [{"text": "no_provider_online"}],
                },
            },
        }
    }


def test_run_finished_is_completed():
    update = agui_event_to_a2a_update({"type": "RUN_FINISHED"}, "task_1", "session_1")

    assert update == {
        "statusUpdate": {
            "taskId": "task_1",
            "contextId": "session_1",
            "status": {"state": "TASK_STATE_COMPLETED"},
        }
    }


def test_text_content_becomes_an_appending_artifact_update():
    update = agui_event_to_a2a_update(
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "hel"}, "task_1", "session_1"
    )

    assert update == {
        "artifactUpdate": {
            "taskId": "task_1",
            "contextId": "session_1",
            "artifact": {"artifactId": "m1", "parts": [{"text": "hel"}]},
            "append": True,
        }
    }


def test_unmodeled_event_falls_back_to_a_working_update():
    event = {"type": "CUSTOM", "name": "sub_agent_progress", "value": {"sub_agent": "translator"}}

    update = agui_event_to_a2a_update(event, "task_1", "session_1")["statusUpdate"]

    assert update["status"]["state"] == "TASK_STATE_WORKING"
    assert update["metadata"]["agui_event"] == event


def test_run_statuses_map_to_a2a_states():
    assert state_for_run_status("queued") == pb.TaskState.TASK_STATE_SUBMITTED
    assert state_for_run_status("running") == pb.TaskState.TASK_STATE_WORKING
    assert state_for_run_status("input-required") == pb.TaskState.TASK_STATE_INPUT_REQUIRED
    assert state_for_run_status("completed") == pb.TaskState.TASK_STATE_COMPLETED
    assert state_for_run_status("failed") == pb.TaskState.TASK_STATE_FAILED
    assert state_for_run_status("cancelled") == pb.TaskState.TASK_STATE_CANCELED
    assert state_for_run_status("cancelling") == pb.TaskState.TASK_STATE_UNSPECIFIED


def test_status_update_from_a_persisted_status_has_no_final_flag():
    update = status_update_for_run_status("t1", "s1", "completed")

    assert update == {
        "statusUpdate": {
            "taskId": "t1",
            "contextId": "s1",
            "status": {"state": "TASK_STATE_COMPLETED"},
        }
    }


def test_inbound_parts_are_read_under_every_spec_version():
    current = a2a_message_to_agui_messages({"role": "ROLE_USER", "parts": [{"text": "hi"}]})
    v0_3 = a2a_message_to_agui_messages({"role": "user", "parts": [{"kind": "text", "text": "hi"}]})
    original = a2a_message_to_agui_messages({"role": "user", "parts": [{"type": "text", "text": "hi"}]})

    as_content = [{"role": m["role"], "content": m["content"]} for m in (current[0], v0_3[0], original[0])]
    assert as_content == [{"role": "user", "content": "hi"}] * 3
    assert current[0]["id"] == "unset"


def test_an_agent_role_is_recognised_under_either_spelling():
    assert a2a_message_to_agui_messages({"role": "ROLE_AGENT", "parts": []})[0]["role"] == "assistant"
    assert a2a_message_to_agui_messages({"role": "agent", "parts": []})[0]["role"] == "assistant"


def test_build_task_merges_a_message_into_one_artifact():
    events = [
        {"type": "RUN_STARTED"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Hello "},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "world"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m2", "delta": "and again"},
        {"type": "RUN_FINISHED"},
    ]

    task = build_task("task_1", "session_1", "translator", "completed", events)

    assert task == {
        "id": "task_1",
        "contextId": "session_1",
        "status": {"state": "TASK_STATE_COMPLETED"},
        "artifacts": [
            {"artifactId": "m1", "parts": [{"text": "Hello world"}]},
            {"artifactId": "m2", "parts": [{"text": "and again"}]},
        ],
    }
