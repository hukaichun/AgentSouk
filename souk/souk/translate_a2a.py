"""Translation between A2A (JSON-RPC task protocol) and AG-UI (event stream).

This is a pragmatic v1 mapping, not a byte-for-byte implementation of every
corner of either spec: A2A Message.parts are reduced to plain text (no
file/data parts), and every AG-UI event that isn't one of the handful of
lifecycle/text-content types funnels into a generic "working" status update
carrying the raw AG-UI event as metadata — so nothing silently disappears
(this is exactly how sub-agent CUSTOM progress events stay visible to A2A
callers, not just AG-UI ones), even if it isn't specially modeled yet.
"""

from __future__ import annotations

from typing import Any

from souk.ids import new_id


def a2a_message_to_agui_messages(a2a_message: dict[str, Any]) -> list[dict[str, Any]]:
    role = "assistant" if a2a_message.get("role") == "agent" else "user"
    text = "".join(
        part.get("text", "") for part in a2a_message.get("parts", []) if part.get("type") == "text"
    )
    return [{"id": new_id("msg"), "role": role, "content": text}]


def agui_event_to_a2a_update(event: dict[str, Any], task_id: str) -> dict[str, Any]:
    event_type = event.get("type")

    if event_type == "RUN_STARTED":
        return _status_update(task_id, "working", final=False)

    if event_type == "RUN_FINISHED":
        return _status_update(task_id, "completed", final=True)

    if event_type == "RUN_ERROR":
        return _status_update(task_id, "failed", final=True, message=event.get("message"))

    if event_type in ("TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_CHUNK"):
        delta = event.get("delta") or event.get("content") or ""
        return {
            "id": task_id,
            "artifact": {"parts": [{"type": "text", "text": delta}]},
        }

    # Fallback: surface every other AG-UI event (tool calls, state deltas,
    # CUSTOM sub-agent progress, ...) as a non-final working update instead
    # of dropping it.
    return _status_update(task_id, "working", final=False, agui_event=event)


def _status_update(
    task_id: str, state: str, *, final: bool, message: Any = None, agui_event: dict[str, Any] | None = None
) -> dict[str, Any]:
    status: dict[str, Any] = {"state": state}
    if message is not None:
        status["message"] = message
    if agui_event is not None:
        status["metadata"] = {"agui_event": agui_event}
    return {"id": task_id, "status": status, "final": final}


def build_task(
    task_id: str, agent_name: str, run_status: str, run_events: list[dict[str, Any]]
) -> dict[str, Any]:
    state_by_run_status = {
        "queued": "submitted",
        "running": "working",
        "completed": "completed",
        "failed": "failed",
        "cancelled": "canceled",
    }
    artifacts = [
        update["artifact"]
        for event in run_events
        if (update := agui_event_to_a2a_update(event, task_id)).get("artifact")
    ]
    return {
        "id": task_id,
        "status": {"state": state_by_run_status.get(run_status, "unknown")},
        "artifacts": artifacts,
    }
