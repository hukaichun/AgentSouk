from __future__ import annotations

from typing import Any


def reduce_events_to_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Folds a stream of AG-UI events into the thread-history messages they represent, in the
    order each message first appeared. Text-message events accumulate into one assistant
    message per `messageId`; tool-call events accumulate into the parent message's `toolCalls`
    (or their own standalone message if there is no parent); a `TOOL_CALL_RESULT` becomes its
    own tool message. Lifecycle and state events (e.g. `RUN_STARTED`, `STATE_DELTA`) produce
    nothing."""
    messages: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    tool_call_parent: dict[str, str] = {}

    def assistant_message(message_id: str, role: str = "assistant") -> dict[str, Any]:
        if message_id not in messages:
            messages[message_id] = {"id": message_id, "role": role, "content": ""}
            order.append(message_id)
        return messages[message_id]

    for event in events:
        etype = event.get("type")

        if etype == "TEXT_MESSAGE_START":
            assistant_message(event["messageId"], event.get("role", "assistant"))

        elif etype == "TEXT_MESSAGE_CONTENT":
            assistant_message(event["messageId"])["content"] += event.get("delta", "")

        elif etype == "TOOL_CALL_START":
            tool_call_id = event["toolCallId"]
            parent_id = event.get("parentMessageId") or tool_call_id
            tool_call_parent[tool_call_id] = parent_id
            msg = assistant_message(parent_id)
            msg.setdefault("toolCalls", []).append(
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {"name": event["toolCallName"], "arguments": ""},
                }
            )

        elif etype == "TOOL_CALL_ARGS":
            tool_call_id = event["toolCallId"]
            parent_id = tool_call_parent.get(tool_call_id)
            if parent_id is None:
                continue
            for tool_call in messages[parent_id].get("toolCalls", []):
                if tool_call["id"] == tool_call_id:
                    tool_call["function"]["arguments"] += event.get("delta", "")
                    break

        elif etype == "TOOL_CALL_RESULT":
            message_id = event["messageId"]
            messages[message_id] = {
                "id": message_id,
                "role": "tool",
                "content": event.get("content", ""),
                "toolCallId": event["toolCallId"],
            }
            order.append(message_id)


    return [messages[message_id] for message_id in order]
