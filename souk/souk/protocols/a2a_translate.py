"""Translation between A2A (JSON-RPC task protocol) and AG-UI (event stream).

This is a pragmatic mapping, not a byte-for-byte implementation of every
corner of either spec: A2A Message.parts are reduced to plain text (no
file/data parts), and every AG-UI event that isn't one of the handful of
lifecycle/text-content types funnels into a generic "working" status update
carrying the raw AG-UI event as metadata — so nothing silently disappears
(this is exactly how sub-agent CUSTOM progress events stay visible to A2A
callers, not just AG-UI ones), even if it isn't specially modeled yet.

**Wire vocabulary is the current A2A spec's** (`kind` discriminators,
`taskId`/`contextId` on stream events, `artifactId` on artifacts). souk used
to emit the original spelling — `{"type": "text"}` parts and `{"id": ...}`
on update events — which no longer matches any client built against the
published schema. What souk *accepts* is deliberately wider than what it
emits: an older caller sending `{"type": "text"}` still works, because
being lenient inbound costs one `or` and being strict inbound breaks
callers for nothing.

None of these shapes are checked against a library, because there isn't one
here: souk depends on `ag-ui-protocol` for AG-UI and hand-writes A2A, so
nothing tells you the spec moved. Everything below was read off
`a2a-sdk`'s published models rather than from memory — see
tests/test_a2a_translate.py, which pins the exact wire shapes.
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
    # souk/schema.py) — from an external A2A caller's perspective its wait
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
        # `kind` is the current spec's discriminator; `type` was the
        # original one and is still what souk's own older clients send.
        part.get("text", "")
        for part in a2a_message.get("parts", [])
        if (part.get("kind") or part.get("type")) == "text"
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
            "kind": "artifact-update",
            "taskId": task_id,
            "contextId": context_id,
            "artifact": {
                # AG-UI's own messageId, so every delta of one assistant
                # message lands in one artifact instead of a new artifact per
                # token. `append` says exactly that: each chunk extends the
                # artifact this id already named, and the first one creates
                # it. Falls back to a per-task id for an event that carries
                # no messageId, which keeps those chunks together too.
                "artifactId": event.get("messageId") or f"{task_id}-text",
                "parts": [{"kind": "text", "text": delta}],
            },
            "append": True,
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
        # A2A's TaskStatus.message is a Message, not a string — an AG-UI
        # RUN_ERROR's `message` is the string, so it gets wrapped rather
        # than dropped into a field whose schema it doesn't fit. The id is
        # derived from the task so it stays stable if the same status is
        # rebuilt from persisted state.
        status["message"] = {
            "kind": "message",
            "messageId": f"{task_id}-error",
            "role": "agent",
            "parts": [{"kind": "text", "text": str(message)}],
        }
    if agui_event is not None:
        status["metadata"] = {"agui_event": agui_event}
    return {
        "kind": "status-update",
        "taskId": task_id,
        "contextId": context_id,
        "status": status,
        "final": final,
    }


def _join_text(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Adjacent text parts become one. The stream sends a part per delta
    because that is what arrived; a *finished* Task holding one part per
    token is a transcript of the streaming, not the answer. Only adjacent
    ones, so a future file/data part between two runs of text keeps them
    apart.
    """
    joined: list[dict[str, Any]] = []
    for part in parts:
        previous = joined[-1] if joined else None
        if previous is not None and previous.get("kind") == "text" and part.get("kind") == "text":
            joined[-1] = {**previous, "text": previous["text"] + part["text"]}
        else:
            joined.append(part)
    return joined


def build_task(
    task_id: str, context_id: str, agent_name: str, run_status: str, run_events: list[dict[str, Any]]
) -> dict[str, Any]:
    """`task_id` here is just souk's own run_id (see protocols.a2a's _start_run —
    there's no separate task_id concept); `context_id` is souk's real,
    database-generated thread_id (see repo.ensure_thread). Both are
    echoed back here (A2A's own `Task.id`/`Task.contextId` fields)
    regardless of whatever the caller originally sent — the only way a
    caller learns the real ones to reuse on its next call.
    """
    # Merged by artifactId, in first-seen order: the stream sends one
    # append-update per delta, but a finished Task should carry one artifact
    # per assistant message with its text whole. A caller reading `artifacts`
    # here used to get one artifact per token.
    merged: dict[str, dict[str, Any]] = {}
    for event in run_events:
        artifact = agui_event_to_a2a_update(event, task_id, context_id).get("artifact")
        if not artifact:
            continue
        existing = merged.get(artifact["artifactId"])
        if existing is None:
            merged[artifact["artifactId"]] = dict(artifact)
        else:
            existing["parts"] = _join_text(existing["parts"] + artifact["parts"])
    return {
        "kind": "task",
        "id": task_id,
        "contextId": context_id,
        "status": {"state": RUN_STATUS_TO_A2A_STATE.get(run_status, "unknown")},
        "artifacts": list(merged.values()),
    }
