from __future__ import annotations

from typing import Any

from ag_ui.core import RunAgentInput
from pydantic import ValidationError

from funduq.ids import new_id

_MESSAGE_ID_EVENT_TYPES = {
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_CHUNK",
    "TEXT_MESSAGE_END",
}


def rewrite_message_ids(event: dict[str, Any], id_map: dict[str, str]) -> dict[str, Any]:
    """Replaces a text-message event's `messageId` with a funduq-issued id, reusing the same
    funduq id for every event sharing the original id (tracked in `id_map`). Events outside the
    text-message-lifecycle types, or without a `messageId`, are returned unchanged."""
    if event.get("type") not in _MESSAGE_ID_EVENT_TYPES:
        return event
    original = event.get("messageId")
    if not original:
        return event
    funduq_id = id_map.setdefault(original, new_id("msg"))
    return {**event, "messageId": funduq_id}


def build_run_agent_input(
    thread_id: str,
    run_id: str,
    messages: list[dict[str, Any]],
    state: Any = None,
    tools: list[dict[str, Any]] | None = None,
    context: list[dict[str, Any]] | None = None,
    forwarded_props: Any = None,
    resume: list[dict[str, Any]] | None = None,
    parent_run_id: str | None = None,
) -> dict[str, Any]:
    """Builds and validates a wire-format AG-UI `RunAgentInput` dict from the given fields,
    raising `ValueError` if the assembled input fails AG-UI's own validation.

    `parent_run_id` is AG-UI's own field for placing another run's id on this
    input. The id appearing a second time is the entire semantics: the caller
    says this run follows that one. Whether it lands as a continuation or an
    interjection is decided by the one fact funduq cannot hold — whether the
    parent is still in flight — which the agent reads off its own loop."""
    try:
        model = RunAgentInput.model_validate(
            {
                "threadId": thread_id,
                "runId": run_id,
                "state": state,
                "messages": messages,
                "tools": tools or [],
                "context": context or [],
                "forwardedProps": forwarded_props,
                "resume": resume,
                "parentRunId": parent_run_id,
            }
        )
    except ValidationError as e:
        raise ValueError(f"invalid AG-UI run input: {e}") from e
    return model.model_dump(mode="json", by_alias=True)
