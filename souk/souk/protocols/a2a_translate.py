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
    return RUN_STATUS_TO_A2A_STATE.get(run_status, pb.TaskState.TASK_STATE_UNSPECIFIED)


def status_update_for_run_status(task_id: str, context_id: str, run_status: str) -> dict[str, Any]:
    return _status_update(task_id, context_id, state_for_run_status(run_status))


def a2a_message_to_agui_messages(a2a_message: dict[str, Any]) -> list[dict[str, Any]]:
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
    if event.get("type") not in ("TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_CHUNK"):
        return None
    return event.get("messageId") or "text", event.get("delta") or event.get("content") or ""


def agui_event_to_a2a_update(event: dict[str, Any], task_id: str, context_id: str) -> dict[str, Any]:
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
