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


def build_run_agent_input(
    thread_id: str,
    run_id: str,
    messages: list[dict[str, Any]],
    state: Any = None,
    tools: list[dict[str, Any]] | None = None,
    context: list[dict[str, Any]] | None = None,
    forwarded_props: Any = None,
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
            }
        )
    except ValidationError as e:
        raise ValueError(f"invalid AG-UI run input: {e}") from e
    return model.model_dump(mode="json", by_alias=True)
