from __future__ import annotations

from typing import Any

from a2a.types import a2a_pb2 as pb
from ag_ui.core import AssistantMessage, UserMessage
from google.protobuf.json_format import MessageToDict

_PLACEHOLDER_MESSAGE_ID = "unset"

RUN_STATUS_TO_A2A_STATE = {
    "queued": pb.TaskState.TASK_STATE_SUBMITTED,
    "running": pb.TaskState.TASK_STATE_WORKING,
    "input-required": pb.TaskState.TASK_STATE_INPUT_REQUIRED,
    "resumed": pb.TaskState.TASK_STATE_COMPLETED,
    "completed": pb.TaskState.TASK_STATE_COMPLETED,
    "failed": pb.TaskState.TASK_STATE_FAILED,
    "cancelled": pb.TaskState.TASK_STATE_CANCELED,
}

TERMINAL_STATES = frozenset(
    {pb.TaskState.TASK_STATE_COMPLETED, pb.TaskState.TASK_STATE_FAILED, pb.TaskState.TASK_STATE_CANCELED}
)


def to_wire(message) -> dict[str, Any]:
    return MessageToDict(message)


def state_for_run_status(run_status: str):
    """Maps a souk run status to its A2A `TaskState`, or `TASK_STATE_UNSPECIFIED` for a status
    (e.g. `"cancelling"`) with no A2A equivalent."""
    return RUN_STATUS_TO_A2A_STATE.get(run_status, pb.TaskState.TASK_STATE_UNSPECIFIED)


def status_update_for_run_status(task_id: str, context_id: str, run_status: str) -> dict[str, Any]:
    """Builds a `TaskStatusUpdateEvent` wire payload reflecting a run's persisted status, with
    no message or metadata attached."""
    return _status_update(task_id, context_id, state_for_run_status(run_status))


def a2a_message_to_agui_messages(a2a_message: dict[str, Any]) -> list[dict[str, Any]]:
    """Converts one inbound A2A `Message` into a one-element list of AG-UI message dicts,
    reading its text parts under any A2A spec version's part shape (`text`/`kind: text`/
    `type: text`) and mapping an agent-authored message to an assistant role, otherwise user.
    The returned message's id is a placeholder, not derived from the A2A message."""
    raw_role = str(a2a_message.get("role", "")).upper()
    text = "".join(
        part["text"] for part in a2a_message.get("parts", []) if isinstance(part.get("text"), str)
    )
    message = (
        AssistantMessage(id=_PLACEHOLDER_MESSAGE_ID, content=text)
        if raw_role in ("ROLE_AGENT", "AGENT")
        else UserMessage(id=_PLACEHOLDER_MESSAGE_ID, content=text)
    )
    return [message.model_dump(mode="json", by_alias=True, exclude_none=True)]


def text_delta_of(event: dict[str, Any]) -> tuple[str, str] | None:
    """Returns `(messageId, text)` if `event` is a text-content AG-UI event, else None."""
    if event.get("type") not in ("TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_CHUNK"):
        return None
    return event.get("messageId") or "text", event.get("delta") or event.get("content") or ""


def agui_event_to_a2a_update(event: dict[str, Any], task_id: str, context_id: str) -> dict[str, Any]:
    """Translates one AG-UI run event into an A2A `StreamResponse` wire payload: run lifecycle
    events become status updates (`RUN_STARTED`->working, `RUN_FINISHED`->completed,
    `RUN_ERROR`->failed with the error message attached), text-content events become appending
    artifact updates keyed by message id, and anything else falls back to a working status
    update carrying the raw AG-UI event under `metadata.agui_event` so it isn't silently
    dropped."""
    event_type = event.get("type")

    if event_type == "RUN_STARTED":
        return _status_update(task_id, context_id, pb.TaskState.TASK_STATE_WORKING)

    if event_type == "RUN_FINISHED":
        return _status_update(task_id, context_id, pb.TaskState.TASK_STATE_COMPLETED)

    if event_type == "RUN_ERROR":
        return _status_update(
            task_id, context_id, pb.TaskState.TASK_STATE_FAILED, message=event.get("message")
        )

    delta = text_delta_of(event)
    if delta is not None:
        artifact_id, text = delta
        return to_wire(
            pb.StreamResponse(
                artifact_update=pb.TaskArtifactUpdateEvent(
                    task_id=task_id,
                    context_id=context_id,
                    artifact=pb.Artifact(artifact_id=artifact_id, parts=[pb.Part(text=text)]),
                    append=True,
                )
            )
        )

    return _status_update(task_id, context_id, pb.TaskState.TASK_STATE_WORKING, agui_event=event)


def _status_update(
    task_id: str,
    context_id: str,
    state,
    *,
    message: Any = None,
    agui_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = pb.TaskStatus(state=state)
    if message is not None:
        status.message.CopyFrom(
            pb.Message(
                message_id=f"{task_id}-error",
                role=pb.Role.ROLE_AGENT,
                parts=[pb.Part(text=str(message))],
            )
        )
    update = pb.TaskStatusUpdateEvent(task_id=task_id, context_id=context_id, status=status)
    if agui_event is not None:
        update.metadata.update({"agui_event": agui_event})
    return to_wire(pb.StreamResponse(status_update=update))


def build_task(
    task_id: str, context_id: str, agent_name: str, run_status: str, run_events: list[dict[str, Any]]
) -> dict[str, Any]:
    """Builds an A2A `Task` wire payload from a run's stored status and event history, merging
    each message's text-content deltas (in event order) into one artifact per `messageId`."""
    merged: dict[str, list[str]] = {}
    for event in run_events:
        delta = text_delta_of(event)
        if delta is None:
            continue
        artifact_id, text = delta
        merged.setdefault(artifact_id, []).append(text)

    return to_wire(
        pb.Task(
            id=task_id,
            context_id=context_id,
            status=pb.TaskStatus(state=state_for_run_status(run_status)),
            artifacts=[
                pb.Artifact(artifact_id=artifact_id, parts=[pb.Part(text="".join(chunks))])
                for artifact_id, chunks in merged.items()
            ],
        )
    )
