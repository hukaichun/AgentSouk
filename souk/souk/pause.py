"""A provider that needs something only a human/caller can supply (tool-
call approval, missing information) before a run can continue ends its
stream with AG-UI's own native interrupt mechanism instead of finishing
normally — this is deliberately not a souk invention.

A regular `RUN_FINISHED` event whose `outcome` is `{"type": "interrupt",
"interrupts": [...]}` (`ag_ui.core.RunFinishedInterruptOutcome`/
`Interrupt`, part of the AG-UI spec itself, ag-ui-protocol >= 0.1.19) is
what a provider emits. Frameworks that already speak AG-UI's interrupt/
resume (e.g. pydantic-ai's `Tool(requires_approval=True)` — see
`providers/pydantic-ai-agent` for where this would go) emit and consume
this automatically; a provider using one of those needs zero souk-
specific code to support pausing. souk's only job is to notice this
outcome while relaying (`grpc_server._handle_relay`) and, once the
stream ends, record `status='input-required'` with the interrupts
preserved in the run's metadata (`grpc_server._handle_finish`) instead
of `status='completed'`.

Resuming is equally native: a caller sends a normal AG-UI call on the
same thread with `resume: [{"interruptId": ..., "status":
"resolved"|"cancelled", "payload": ...}]` — `ag_ui.core.ResumeEntry`,
forwarded to the provider byte-for-byte (`souk.agui.build_run_agent_input`'s
`resume` param). souk never reads `payload`; it only checks that the list
is non-empty to allow restarting a thread that already has an active
(paused) run — see `api_agui._run_agent`. **Your run keeps its same
`run_id` (and A2A `task_id`, if any) for the new round** — `run_stream`
gets invoked again with a fresh `RunAgentInput` on that same id, not
handed off to a new one.

A run genuinely cannot make further progress without this being
resolved — that's the whole point of `input-required`: it blocks a new
run from starting on the same thread (see `repo.get_active_run_for_thread`)
until the interrupt is addressed.

**Only ever through AG-UI, never through A2A — this is a provider
decision, not a protocol one.** A provider that pauses decides, itself,
whether the human/caller on the other end can actually supply what it's
waiting for. If the call reaching it came in via A2A, "the other end" is
just another agent, not a human — there's no reason to let *that* agent
resolve an interrupt it was never meant to approve in the first place.
So `is_resuming` below is the one and only check either surface uses to
decide whether to let a call bypass an active, paused run instead of
just reporting its current state — `api_agui._run_agent` feeds it the
real `resume` a caller sent (AG-UI is the one surface that legitimately
can carry one); `api_a2a._start_run` always feeds it `None`, structurally
incapable of ever bypassing this. An A2A-invoked provider that needs a
human to actually resolve something must design for that human reaching
it directly over AG-UI on this same thread_id — not for the delegating
agent to do it on the human's behalf.

Waiting on a sub-agent call to resolve is a different thing entirely,
and deliberately does *not* go through this module: whether one agent
can get an answer from another it delegated to is just a question of
whether *that callee's own thread* can currently accept a new run (the
same `get_active_run_for_thread` check, applied to the callee's thread
instead of the caller's) — not something that needs the caller's own
thread blocked, subscribed, or notified. See `providers/pydantic-ai-agent/
pydantic_ai_agent/sub_agent_tool.py` for how a delegating agent checks
back on a callee: it just calls again:

- Callee's thread has no active run (it genuinely finished) → the call
  goes through for real, with the real answer.
- Callee's thread still has one (still running, or itself paused) → the
  call doesn't start a second one; souk hands back the callee's current
  state instead (`api_a2a._finalize_delegated_call`). The delegating
  agent reports this honestly ("still pending, don't call me again for
  this") and finishes its *own* run normally — no connection held open,
  no subscription registered, no proactive wake-up. It (or whoever
  prompts it next — a new message, its own next natural turn) simply
  asks again whenever it next has reason to.
"""

from typing import Any


def interrupt_outcome_of(event: dict) -> list[dict[str, Any]] | None:
    """The `interrupts` list if `event` is a RUN_FINISHED carrying AG-UI's
    native `outcome={"type": "interrupt", ...}`, else None. A provider
    that finishes a run normally (`outcome` absent, or `{"type":
    "success"}`) gets None — the ordinary "completed" case.
    """
    if event.get("type") != "RUN_FINISHED":
        return None
    outcome = event.get("outcome")
    if not isinstance(outcome, dict) or outcome.get("type") != "interrupt":
        return None
    return outcome.get("interrupts") or []


def is_resuming(active_run: dict[str, Any] | None, resume: list[dict[str, Any]] | None) -> bool:
    """True only if `active_run` is genuinely paused (`input-required`)
    and `resume` is real, non-empty AG-UI `ResumeEntry` data — the one
    condition either HTTP surface uses to decide whether a call may
    bypass an active run instead of just reporting its current state
    (see `repo.get_active_run_for_thread`'s callers). `api_a2a._start_run`
    always passes `resume=None` here — see this module's docstring for
    why that's not an oversight.
    """
    return (
        active_run is not None
        and bool(resume)
        and active_run["status"] == "input-required"
    )
