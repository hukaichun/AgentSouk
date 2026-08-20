from __future__ import annotations

from souk.agui_reduce import reduce_events_to_messages


def test_reduces_a_plain_text_reply_into_one_assistant_message():
    events = [
        {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "Hello, "},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "world!"},
        {"type": "TEXT_MESSAGE_END", "messageId": "m1"},
    ]
    assert reduce_events_to_messages(events) == [
        {"id": "m1", "role": "assistant", "content": "Hello, world!"}
    ]


def test_reduces_a_tool_call_and_its_result_into_assistant_plus_tool_messages():
    events = [
        {
            "type": "TOOL_CALL_START",
            "toolCallId": "call_1",
            "toolCallName": "list_souk_agents",
            "parentMessageId": "m1",
        },
        {"type": "TOOL_CALL_ARGS", "toolCallId": "call_1", "delta": '{"include_offline"'},
        {"type": "TOOL_CALL_ARGS", "toolCallId": "call_1", "delta": ": true}"},
        {"type": "TOOL_CALL_END", "toolCallId": "call_1"},
        {
            "type": "TOOL_CALL_RESULT",
            "messageId": "tool_1",
            "toolCallId": "call_1",
            "content": "- souk-guide (online)",
        },
        {"type": "TEXT_MESSAGE_START", "messageId": "m2", "role": "assistant"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m2", "delta": "Here you go."},
        {"type": "TEXT_MESSAGE_END", "messageId": "m2"},
    ]
    assert reduce_events_to_messages(events) == [
        {
            "id": "m1",
            "role": "assistant",
            "content": "",
            "toolCalls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "list_souk_agents",
                        "arguments": '{"include_offline": true}',
                    },
                }
            ],
        },
        {
            "id": "tool_1",
            "role": "tool",
            "content": "- souk-guide (online)",
            "toolCallId": "call_1",
        },
        {"id": "m2", "role": "assistant", "content": "Here you go."},
    ]


def test_tool_call_with_no_parent_message_id_gets_its_own_standalone_message():
    events = [
        {"type": "TOOL_CALL_START", "toolCallId": "call_1", "toolCallName": "foo"},
        {"type": "TOOL_CALL_ARGS", "toolCallId": "call_1", "delta": "{}"},
        {"type": "TOOL_CALL_END", "toolCallId": "call_1"},
    ]
    result = reduce_events_to_messages(events)
    assert len(result) == 1
    assert result[0]["id"] == "call_1"
    assert result[0]["toolCalls"][0]["function"]["arguments"] == "{}"


def test_ignores_lifecycle_and_state_events():
    events = [
        {"type": "RUN_STARTED", "threadId": "t1", "runId": "r1"},
        {"type": "STEP_STARTED", "stepName": "thinking"},
        {"type": "STATE_DELTA", "delta": []},
        {"type": "RUN_FINISHED", "threadId": "t1", "runId": "r1", "outcome": {"type": "success"}},
    ]
    assert reduce_events_to_messages(events) == []
