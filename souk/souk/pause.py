"""Convention for a provider to signal that a run has entered a paused,
resumable state (HITL tool-call approval, or waiting on another thread —
typically a sub-agent call — to resolve) instead of finishing normally.

This is deliberately *not* a new wire protocol: it's a regular AG-UI
CUSTOM event, so nothing about AG-UI framing or the gRPC envelope
changes. A provider that wants to pause a run emits this CUSTOM event
at some point during the run and then still ends the stream normally
(RUN_FINISHED / end_of_stream=true) — a paused run never holds the
connection open (see souk.health for why that would be indistinguishable
from a stalled/broken provider).

souk watches for this event while relaying (souk.grpc_server._relay_event)
and, when the run's stream ends, records status='input-required' with
`value` merged into the run's metadata instead of status='completed'
(souk.grpc_server._finish_run).

`value.waitingOnRunId`, if set, marks this run as waiting on a *specific*
other run resolving (the sub-agent-call case) — this is what lets souk
automatically resume this run once that run reaches a terminal state
(see repo.find_run_waiting_on / grpc_server._resume_parent_run_if_waiting).
Leave it unset for a plain HITL pause with no sub-call involved.

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
"""

PAUSE_EVENT_NAME = "souk.run_paused"


def is_pause_event(event: dict) -> bool:
    return event.get("type") == "CUSTOM" and event.get("name") == PAUSE_EVENT_NAME
