from __future__ import annotations

from typing import Any

from ag_ui.core import RunAgentInput
from pydantic import ValidationError

from souk.ids import new_id

_MESSAGE_ID_EVENT_TYPES = {
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_CHUNK",
    "TEXT_MESSAGE_END",
}


def rewrite_message_ids(event: dict[str, Any], id_map: dict[str, str]) -> dict[str, Any]:
    if event.get("type") not in _MESSAGE_ID_EVENT_TYPES:
        return event
    original = event.get("messageId")
    if not original:
        return event
    souk_id = id_map.setdefault(original, new_id("msg"))
    return {**event, "messageId": souk_id}


def build_run_agent_input(
    thread_id: str,
    run_id: str,
    messages: list[dict[str, Any]],
    state: Any = None,
    tools: list[dict[str, Any]] | None = None,
    context: list[dict[str, Any]] | None = None,
    forwarded_props: Any = None,
    resume: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
            }
        )
    except ValidationError as e:
        raise ValueError(f"invalid AG-UI run input: {e}") from e
    return model.model_dump(mode="json", by_alias=True)
