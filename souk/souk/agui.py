"""Builds the AG-UI RunAgentInput JSON actually delivered to an agent.

The inbound HTTP request body (api_agui.py) is already the real
`ag_ui.core.RunAgentInput` — no separate, souk-flavored model to
translate from (see souk/models.py's module docstring). This module's
job is just reassembling the pieces souk itself decides (the real
thread_id/run_id, database-generated message ids, forwardedProps merged
with any KYOK addition) into that same real schema, re-validated here so
a malformed result 400s at souk with a clear error instead of failing
silently deep inside the agent process (it's re-validated *again* there,
by pydantic-ai's AGUIAdapter, against the same ag-ui-protocol schema).
"""

from __future__ import annotations

from typing import Any

from ag_ui.core import RunAgentInput
from pydantic import ValidationError

from souk.ids import new_id

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
    sees (thread_id, run_id, agent_id, inbound message ids — the last via
    repo.append_thread_messages, a real database-generated id), so a raw
    provider uuid leaking through here is the one inconsistency left. Not
    a database id itself — an assistant reply is never persisted as a
    thread_history message row (see that table's module docstring in
    souk/schema.py), only relayed live — so there's no row for a database
    default to generate this from; `new_id` is the same souk-assigned
    scheme applied in memory instead. `id_map` is a per-stream dict the
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
