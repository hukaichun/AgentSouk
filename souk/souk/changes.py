"""What changed, for anything watching a Souk from inside the process.

souk already computes these facts — an agent registered, a run went to
`running` — but until now the only way to learn one was to ask again. An
adapter that has to tell its own clients "the toolbox changed" was left
polling `list_agents()` on a timer.

Deliberately coarse. `RosterChanged` says *something* about the roster is
different, not what; the subscriber re-queries. Fine-grained events would
mean promising a complete, ordered account of every change, which is a much
larger promise than "look again" — and one souk cannot keep across a restart
or a second process anyway.

Frozen dataclasses rather than pydantic models, unlike `souk/models.py`: this
is a signal, not state souk hands out. Nothing serialises a `ChangeEvent` —
downstream reacts to it and then queries the models, which is where the
wire-facing shapes live.

Delivery has the same honesty as `claim_work`'s `on_cancel`: a plain
synchronous call, not awaited, no queue, no retry, no ordering guarantee
across subscribers. Souk tells you and carries on; if you were not
subscribed at the time, you missed it, and the database remains the thing
that is true.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RosterChanged:
    """Which agents exist, or who is serving them, is different: a
    registration, a re-registration that delisted a name by omitting it, or a
    provider attaching or detaching.

    Not fired for an agent crossing the online/offline line by going quiet.
    That is a derived fact — `last_seen_at` compared against
    `online_window_seconds` at the moment you ask — so there is no instant at
    which anything happens to fire on. A watcher that needs it polls; see
    `AgentSummary.online`.
    """


@dataclass(frozen=True)
class RunStatusChanged:
    """One run moved to `status`. Covers every transition souk records,
    including the ones a health sweep decides rather than a worker."""

    run_id: str
    status: str


ChangeEvent = RosterChanged | RunStatusChanged
