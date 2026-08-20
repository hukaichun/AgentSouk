# Support

Part of [core components](../core-components.md).

**Change notifications** (`changes.py` for the event types, `core.py`
for the subscription) — `Souk.on_change(callback)` adds the callback to
a set and returns an unsubscribe function; every state transition worth
showing (roster changed, LLM
roster changed, a run's status changed) constructs a small typed event
and calls each subscriber synchronously. A serving layer subscribes once
and updates what it shows when told — no polling loop, no missed-poll
staleness. Events say *that* something changed; reading the new state is
the subscriber's own query.

**Health sweeps** (`health.py`) — one background loop, started with the
`Souk` object, ticks on an interval and fails what stalled: a claimed
run with no activity past the stall timeout is marked failed by a direct
database update (it works even if the run's provider vanished), and a
paused run past its resume deadline the same way, when that timeout is
configured. A queued run whose agent has been without a serving provider
past its window is failed as "no provider took it" by the dispatch loop
itself, not this sweep — `expire_queued` in `broker.py`, where the
window (a broker argument, 45 s by default) lives; while a provider is
attached, a queued run waits indefinitely. All of these write a reason
and a terminal event; none
guesses an outcome — the sweeps time out on the *absence* of one, which
is itself an observation. `Souk.health()` is the companion snapshot:
database reachable, schema at the expected revision, dispatch loop
alive.

**Settings** (`config.py`) — `CoreSettings`, a pydantic-settings object
reading environment variables under the `SOUK_` prefix, constructed once
and handed to `Souk`. Every deliberate switch lives here: database URL
and schema, timeouts and sweep intervals, the token-signing secret,
souk's identity key, and migration-stage switches such as
`require_connect_proof` (attach authentication: the mechanism is always
on, enforcement is flipped per deployment when its transports are
ready).
