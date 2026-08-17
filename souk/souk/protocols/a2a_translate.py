"""Translation between A2A (task protocol) and AG-UI (event stream).

Every A2A shape here is built from `a2a.types.a2a_pb2` — the SDK's own v1.0
definitions — and serialised with protobuf's JSON mapping. Nothing in this
file spells a field name or an enum value by hand, which is the entire point:
souk used to, and went two protocol versions without noticing. It answered
`tasks/send`, emitted `{"type": "text"}` parts and `{"id": ...}` on update
events, and nothing failed until a real client got `-32601`. A rename in the
spec now breaks this file loudly, at import or at build time, instead of
becoming a silent incompatibility.

What souk *emits* is v1.0 and only v1.0. What it accepts is wider — v1.0,
v0.3 and souk's own original spelling — because being lenient inbound costs
an `or` and being strict inbound breaks callers for nothing. That asymmetry
is deliberate; see `a2a_message_to_agui_messages`.

Still a pragmatic mapping rather than a complete one: A2A `Part`s are reduced
to text (no file/data parts), and every AG-UI event that isn't one of the
handful of lifecycle/text types funnels into a generic `WORKING` status
update carrying the raw AG-UI event in `metadata` — so nothing silently
disappears (this is how sub-agent CUSTOM progress events stay visible to A2A
callers), even where it isn't specially modelled.

One thing v0.3 had and v1.0 does not: `final` on an update. Finality is the
stream ending, plus the terminal state in the last status — so souk no longer
sends a flag for it.
"""

from __future__ import annotations

from typing import Any

from a2a.types import a2a_pb2 as pb
from ag_ui.core import AssistantMessage, UserMessage
from google.protobuf.json_format import MessageToDict

# `ag_ui.core.Message.id` is required, but the real id is
# `repo.append_thread_messages`'s to mint — this one is overwritten
# unconditionally before storage and never read. See
# `a2a_message_to_agui_messages`.
_PLACEHOLDER_MESSAGE_ID = "unset"

# souk's run statuses <-> A2A's own task states. 'input-required' is
# already A2A vocabulary (a task legitimately paused waiting on more
# input) — reused as-is rather than inventing a souk-specific name, see
# souk/pause.py.
RUN_STATUS_TO_A2A_STATE = {
    "queued": pb.TaskState.TASK_STATE_SUBMITTED,
    "running": pb.TaskState.TASK_STATE_WORKING,
    "input-required": pb.TaskState.TASK_STATE_INPUT_REQUIRED,
    # souk-specific bookkeeping status with no A2A equivalent (see
    # souk/schema.py) — from an external A2A caller's perspective its wait
    # did resolve, just via a new run/task rather than this one, so
    # COMPLETED is the closest honest answer to GetTask on this id.
    "resumed": pb.TaskState.TASK_STATE_COMPLETED,
    "completed": pb.TaskState.TASK_STATE_COMPLETED,
    "failed": pb.TaskState.TASK_STATE_FAILED,
    "cancelled": pb.TaskState.TASK_STATE_CANCELED,
    # 'cancelling' is deliberately absent: souk has asked and does not yet
    # know the answer, and A2A has no state for that. UNSPECIFIED is the
    # honest one — see protocols/a2a's cancel_task.
}

# Terminal from A2A's point of view. Not sent on the wire (v1.0 has no
# `final` field); souk uses it to decide whether a live watcher needs the
# real outcome after the raw event stream ends.
TERMINAL_STATES = frozenset(
    {pb.TaskState.TASK_STATE_COMPLETED, pb.TaskState.TASK_STATE_FAILED, pb.TaskState.TASK_STATE_CANCELED}
)


def to_wire(message) -> dict[str, Any]:
    """A protobuf message as the JSON A2A puts on the wire. Field names and
    enum spellings come from the descriptor, so they cannot drift from the
    spec without this failing."""
    return MessageToDict(message)


def state_for_run_status(run_status: str):
    return RUN_STATUS_TO_A2A_STATE.get(run_status, pb.TaskState.TASK_STATE_UNSPECIFIED)


def status_update_for_run_status(task_id: str, context_id: str, run_status: str) -> dict[str, Any]:
    """A StreamResponse carrying one status update, built straight from a
    persisted run status — for when there is no live AG-UI event stream to
    translate from (a subscribe on a run that is already paused or finished).
    """
    return _status_update(task_id, context_id, state_for_run_status(run_status))


def a2a_message_to_agui_messages(a2a_message: dict[str, Any]) -> list[dict[str, Any]]:
    """Inbound, and deliberately lenient about which spec version wrote it.

    A text part is `{"text": ...}` in v1.0, `{"kind": "text", "text": ...}` in
    v0.3 and `{"type": "text", "text": ...}` in the original — all three carry
    the text under the same key, so souk reads the key and ignores the
    discriminator entirely. Roles are `ROLE_USER`/`ROLE_AGENT` in v1.0 and
    `user`/`agent` before it. The *input* side stays a bare dict for this
    reason: three A2A spec generations don't share one shape to validate
    against, and rejecting two of them for a discriminator souk doesn't
    need would be exactly the forced protocol deviation this repo avoids.

    The *output* is a different question — this always produces AG-UI's own
    shape, souk's own `RunAgentInput.messages` on the other protocol goes
    through `ag_ui.core` on its way here too (see protocols.agui), and dict
    literals with hand-spelled `role`/`content` keys were the one place that
    type wasn't actually checked. Built through `UserMessage`/`AssistantMessage`
    and dumped, same as `protocols.agui`'s own `raw_messages` — so a rename on
    that side breaks this construction at the same moment, not later at
    whatever field a provider happens to read off the stored dict.

    No real `id` here — `repo.append_thread_messages` assigns the real,
    database-generated one for whatever it stores this under; an id minted
    here would just be discarded, which is what `_PLACEHOLDER_MESSAGE_ID` is.
    """
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
    """(artifact_id, text) for an AG-UI event that carries assistant text,
    None for anything else.

    The artifact id is AG-UI's own messageId, so every delta of one assistant
    message lands in one artifact instead of a new artifact per token.
    """
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
        # `append` is what says these chunks extend the artifact this id
        # already named; the first one creates it.
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

    # Fallback: surface every other AG-UI event (tool calls, state deltas,
    # CUSTOM sub-agent progress, ...) as a non-terminal working update instead
    # of dropping it.
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
        # TaskStatus.message is a Message, not a string — an AG-UI RUN_ERROR
        # carries the string, so it is wrapped rather than dropped into a
        # field whose type it doesn't fit. The id is derived from the task so
        # it stays stable if the same status is rebuilt from persisted state.
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
    """`task_id` here is just souk's own run_id (see protocols.a2a's _start_run —
    there's no separate task_id concept); `context_id` is souk's real,
    database-generated thread_id (see repo.ensure_thread). Both are
    echoed back here (A2A's own `Task.id`/`Task.contextId` fields)
    regardless of whatever the caller originally sent — the only way a
    caller learns the real ones to reuse on its next call.

    Text is merged per assistant message, not per event: the stream sends a
    delta at a time because that is what arrived, but a finished Task holding
    one artifact per token is a transcript of the streaming rather than the
    answer.
    """
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
