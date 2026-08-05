"""Ports AG-UI's own reference client-side reconstruction logic
(`@ag-ui/client`'s `defaultApplyEvents`) to Python: turns a run's raw
event stream back into real `ag_ui.core.AssistantMessage`/`ToolMessage`
objects, using only `messageId`/`toolCallId`/`parentMessageId` — the same
fields any AG-UI-speaking provider is already guaranteed to send, so no
provider-specific cooperation is needed (see souk/pause.py's neighboring
module docstrings for the same "works for any AG-UI agent" principle).

This is what makes `thread_history` (and therefore `GET /threads/
{thread_id}`) an actual source of truth for the full conversation,
including tool calls — not just caller-side messages, which is all it
persisted before (see grpc_server._handle_finish, the one caller of
this).
"""

from __future__ import annotations

from typing import Any


def reduce_events_to_messages(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Returns the AssistantMessage/ToolMessage dicts implied by `events`,
    in the order each first appeared. Ignores every other AG-UI event
    type (RUN_STARTED, STATE_DELTA, STEP_*, ...) — those aren't
    conversation content.
    """
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
            # No parentMessageId means this tool call isn't attached to
            # any text message already being streamed — it still needs
            # some assistant message to live under, so it gets its own
            # (keyed by its own tool_call_id, same as the reference
            # client does for a standalone tool-call turn).
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

        # TEXT_MESSAGE_END / TOOL_CALL_END carry nothing not already
        # implied by the START/CONTENT/ARGS events above — nothing to do.

    return [messages[message_id] for message_id in order]
