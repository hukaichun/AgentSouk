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

# souk's run statuses <-> A2A's own task states. 'input-required' is
# already A2A vocabulary (a task legitimately paused waiting on more
# input) — reused as-is rather than inventing a souk-specific name, see
# souk/pause.py.
RUN_STATUS_TO_A2A_STATE = {
    "queued": "submitted",
    "running": "working",
    "input-required": "input-required",
    # souk-specific bookkeeping status with no A2A equivalent (see
    # souk/db.py) — from an external A2A caller's perspective its wait
    # did resolve, just via a new run/task rather than this one, so
    # 'completed' is the closest honest answer to tasks/get on this id.
    "resumed": "completed",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "canceled",
}


def status_update_for_run_status(task_id: str, context_id: str, run_status: str) -> dict[str, Any]:
    """Builds a TaskStatusUpdateEvent directly from a persisted run status,
    for when there's no live AG-UI event stream to translate from (e.g.
    a tasks/sendSubscribe call on a run that's already paused or finished
    — see api_a2a.rpc's tasks/sendSubscribe handler).
    """
    state = RUN_STATUS_TO_A2A_STATE.get(run_status, "unknown")
    return _status_update(task_id, context_id, state, final=state in ("completed", "failed", "canceled"))


def a2a_message_to_agui_messages(a2a_message: dict[str, Any]) -> list[dict[str, Any]]:
    """No `id` here on purpose — repo.append_thread_messages assigns the
    real, database-generated one for whatever it stores this under; an id
    minted here would just be discarded.
    """
    role = "assistant" if a2a_message.get("role") == "agent" else "user"
    text = "".join(
        part.get("text", "") for part in a2a_message.get("parts", []) if part.get("type") == "text"
    )
    return [{"role": role, "content": text}]


def agui_event_to_a2a_update(event: dict[str, Any], task_id: str, context_id: str) -> dict[str, Any]:
    event_type = event.get("type")

    if event_type == "RUN_STARTED":
        return _status_update(task_id, context_id, "working", final=False)

    if event_type == "RUN_FINISHED":
        return _status_update(task_id, context_id, "completed", final=True)

    if event_type == "RUN_ERROR":
        return _status_update(task_id, context_id, "failed", final=True, message=event.get("message"))

    if event_type in ("TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_CHUNK"):
        delta = event.get("delta") or event.get("content") or ""
        return {
            "id": task_id,
            "artifact": {"parts": [{"type": "text", "text": delta}]},
        }

    # Fallback: surface every other AG-UI event (tool calls, state deltas,
    # CUSTOM sub-agent progress, ...) as a non-final working update instead
    # of dropping it.
    return _status_update(task_id, context_id, "working", final=False, agui_event=event)


def _status_update(
    task_id: str,
    context_id: str,
    state: str,
    *,
    final: bool,
    message: Any = None,
    agui_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status: dict[str, Any] = {"state": state}
    if message is not None:
        status["message"] = message
    if agui_event is not None:
        status["metadata"] = {"agui_event": agui_event}
    return {"id": task_id, "contextId": context_id, "status": status, "final": final}


def build_task(
    task_id: str, context_id: str, agent_name: str, run_status: str, run_events: list[dict[str, Any]]
) -> dict[str, Any]:
    """`task_id` here is just souk's own run_id (see api_a2a._start_run —
    there's no separate task_id concept); `context_id` is souk's real,
    database-generated thread_id (see repo.ensure_thread). Both are
    echoed back here (A2A's own `Task.id`/`Task.contextId` fields)
    regardless of whatever the caller originally sent — the only way a
    caller learns the real ones to reuse on its next call.
    """
    artifacts = [
        update["artifact"]
        for event in run_events
        if (update := agui_event_to_a2a_update(event, task_id, context_id)).get("artifact")
    ]
    return {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": RUN_STATUS_TO_A2A_STATE.get(run_status, "unknown")},
        "artifacts": artifacts,
    }
