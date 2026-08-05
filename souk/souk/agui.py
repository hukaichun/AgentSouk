"""Builds the AG-UI RunAgentInput JSON actually delivered to an agent.

souk's own HTTP request body (souk.models.RunAgentInput) is intentionally
loose and souk-flavored (snake_case, thread_id optional/souk-assigned).
But what gets forwarded to the agent side must be a genuinely valid AG-UI
RunAgentInput — camelCase wire format, every required field present
(state, tools, context, forwardedProps) — since it's re-validated there
by pydantic-ai's AGUIAdapter against the real ag-ui-protocol schema. Doing
that validation here too means a malformed request 400s at souk with a
clear error instead of failing silently deep inside the agent process.
"""

from __future__ import annotations

from typing import Any

from ag_ui.core import RunAgentInput
from pydantic import ValidationError

from souk.ids import new_id


def fill_message_ids(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """AG-UI's own Message schema requires `id` (see ag_ui.core.UserMessage
    — no default), so a caller that omits it would otherwise 400 out of
    build_run_agent_input's validation below. souk already assigns message
    ids on the caller's behalf for A2A-sourced messages
    (translate_a2a.a2a_message_to_agui_messages's own `new_id("msg")`) —
    this is the same thing for direct AG-UI callers, so neither path
    requires a caller to invent its own id just to satisfy the schema.
    A caller that *does* supply one (e.g. to correlate it with something
    on its own side) keeps it untouched.
    """
    return [m if m.get("id") else {**m, "id": new_id("msg")} for m in messages]


# AG-UI event types that carry a `messageId` identifying which streamed
# message they belong to (START/CONTENT/CHUNK/END of the same reply share
# one id) — see the ag-ui-protocol event schema.
_MESSAGE_ID_EVENT_TYPES = {
    "TEXT_MESSAGE_START",
    "TEXT_MESSAGE_CONTENT",
    "TEXT_MESSAGE_CHUNK",
    "TEXT_MESSAGE_END",
}


def rewrite_message_ids(event: dict[str, Any], id_map: dict[str, str]) -> dict[str, Any]:
    """Agents generate their own `messageId` for streamed replies (e.g.
    pydantic-ai's AGUIAdapter mints a plain uuid4) — provider-internal and
    in no way souk's id scheme. souk otherwise assigns every id a caller
    sees (thread_id, run_id, agent_id, and now inbound message ids via
    fill_message_ids above), so a raw provider uuid leaking through here
    is the one inconsistency left. `id_map` is a per-stream dict the
    caller keeps across the whole run so every event for the same
    provider-side messageId gets the same souk-assigned id.
    """
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
                # AG-UI's own field (ag_ui.core.ResumeEntry) — forwarded
                # byte-for-byte from whatever the caller supplied. See
                # souk/pause.py for how a provider's own RUN_FINISHED
                # interrupt outcome round-trips into this.
                "resume": resume,
            }
        )
    except ValidationError as e:
        raise ValueError(f"invalid AG-UI run input: {e}") from e
    return model.model_dump(mode="json", by_alias=True)
