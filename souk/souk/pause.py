"""Two independent ways a run ends up paused/resumable instead of
finishing normally — deliberately not unified into one mechanism, because
they answer different questions:

1. **Plain HITL pause — AG-UI's own native mechanism, not souk's.**
   A provider that needs something only a human/caller can supply (tool-
   call approval, missing information) ends its stream with a regular
   `RUN_FINISHED` event whose `outcome` is
   `{"type": "interrupt", "interrupts": [...]}` — `ag_ui.core.
   RunFinishedInterruptOutcome`/`Interrupt`, part of the AG-UI spec itself
   (ag-ui-protocol >= 0.1.19), not a souk invention. Frameworks that
   already speak AG-UI's interrupt/resume (e.g. pydantic-ai's
   `Tool(requires_approval=True)` — see `providers/pydantic-ai-agent`'s
   tool definitions for where this would go) emit and consume this
   automatically; a provider using one of those needs zero souk-specific
   code to support pausing. souk's only job is to notice this outcome
   while relaying (`grpc_server._handle_relay`) and, once the stream ends,
   record `status='input-required'` with the interrupts preserved in the
   run's metadata (`grpc_server._handle_finish`) instead of
   `status='completed'`.

   Resuming is equally native: a caller sends a normal AG-UI call on the
   same thread with `resume: [{"interruptId": ..., "status":
   "resolved"|"cancelled", "payload": ...}]` — `ag_ui.core.ResumeEntry`,
   forwarded to the provider byte-for-byte
   (`souk.agui.build_run_agent_input`'s `resume` param). souk never reads
   `payload`; it only checks that the list is non-empty to allow
   restarting a thread that already has an active (paused) run — see
   `api_agui._run_agent`. **Your run keeps its same `run_id` (and A2A
   `task_id`, if any) for the new round** — `run_stream` gets invoked
   again with a fresh `RunAgentInput` on that same id, not handed off to
   a new one.

2. **Waiting on a specific sub-agent call — a real souk extension, kept
   as one.** AG-UI's interrupt/resume has no concept of "wake me up
   automatically once some other run finishes" — that's not a caller
   resolving something, it's souk itself deciding to re-invoke a run with
   no external trigger at all. There's no native field to piggyback on,
   so this still goes through a plain AG-UI CUSTOM event
   (`PAUSE_EVENT_NAME` below) that a provider emits before ending its
   stream normally, naming the run_id it's waiting on
   (`value.waitingOnRunId`). souk watches for this the same way, in the
   same handler, and — once that named run reaches a real terminal state
   — auto-resumes this one with no caller involved (see
   `repo.find_run_waiting_on` / `grpc_server._resume_parent_run_if_waiting`).

   Deliberately a run_id, not a thread_id: a callee thread can be reused
   across several unrelated delegation calls over its lifetime (see
   ensure_thread's parent_thread_id reuse), so "waiting on this thread" is
   ambiguous — which of possibly several calls to it does the waiter mean?
   Pinning the specific run_id that was live at the moment of delegation
   removes the ambiguity — and that run_id stays valid for the waiter to
   check even after several pause/resume rounds, since resuming a paused
   run keeps it under its *same* run_id (see repo.reopen_run) rather than
   handing off to a new one; the waiter never needs to re-declare interest
   or have its pointer moved.

   Deliberately single-hop, not transitive: if A calls B calls C, and B
   declares waitingOnRunId=<C's run> while A never declared
   waitingOnRunId=<B's run> (e.g. A called B synchronously and already
   consumed B's immediate reply as B's whole answer), then C finishing
   wakes B, but B's own resumed run finishing does *not* automatically
   notify A — nothing propagates a notification further than whoever
   explicitly declared interest in the run that just resolved. Getting an
   end-to-end notification across a multi-hop chain currently requires
   every hop along the way to independently adopt this same pattern toward
   its own caller; souk itself has no chain-wide bookkeeping and doesn't
   try to guess who, further up, might ultimately care. Known scope
   boundary, not an oversight — revisit only as a deliberate roadmap item,
   not a drive-by fix.

Both cases end their stream normally (RUN_FINISHED / end_of_stream=true)
— a paused run never holds the connection open (see souk.health for why
that would be indistinguishable from a stalled/broken provider).
"""

from typing import Any

PAUSE_EVENT_NAME = "souk.run_paused"


def is_pause_event(event: dict) -> bool:
    """Case 2 only (waiting on a specific sub-agent run) — case 1 (plain
    HITL) is detected by interrupt_outcome_of below, since it isn't a
    CUSTOM event at all.
    """
    return event.get("type") == "CUSTOM" and event.get("name") == PAUSE_EVENT_NAME


def interrupt_outcome_of(event: dict) -> list[dict[str, Any]] | None:
    """The `interrupts` list if `event` is a RUN_FINISHED carrying AG-UI's
    native `outcome={"type": "interrupt", ...}` (case 1 above), else None.
    A provider that finishes a run normally (`outcome` absent, or
    `{"type": "success"}`) gets None — the ordinary "completed" case.
    """
    if event.get("type") != "RUN_FINISHED":
        return None
    outcome = event.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("type") != "interrupt":
        return None
    return outcome.get("interrupts") or []
