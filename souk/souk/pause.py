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

`value.waitingOnThreadId`, if set, marks this run as waiting on another
thread's own run resolving (the sub-agent-call case) — this is what lets
souk automatically resume this run once that thread's run completes (see
repo.find_parent_run_waiting_on / grpc_server._resume_parent_run). Leave
it unset for a plain HITL pause with no sub-thread involved.
"""

PAUSE_EVENT_NAME = "souk.run_paused"


def is_pause_event(event: dict) -> bool:
    return event.get("type") == "CUSTOM" and event.get("name") == PAUSE_EVENT_NAME
