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
whether the database is reachable, which schema revision it found
alongside the one souk expected, and whether the dispatch loop is alive.
It reports those facts and compares nothing — deciding that a revision
mismatch is fatal is the caller's call.

**Settings** (`config.py`) — `CoreSettings`, a pydantic-settings object
reading environment variables under the `SOUK_` prefix. Resolution
happens when `Souk(...)` is constructed, not at import: there is no
module-level singleton.

## Every core setting

The whole list. It is short on purpose — core knows a database and
nothing else, so anything describing a wire is absent by design, not by
omission.

| setting | default | governs |
|---|---|---|
| `database_url` | `sqlite+aiosqlite:///./souk.db` | which database, and whether the engine is built the SQLite way |
| `db_schema` | `public` | the Postgres `search_path`; applied only when the backend is not SQLite and the value is not the default |
| `stale_hidden_window_seconds` | 7 days | how long since last check-in before an agent drops out of the roster listings |
| `run_stall_timeout_seconds` | 120 | a claimed run silent past this is failed `stalled_no_activity` |
| `health_sweep_interval_seconds` | 15 | how often the sweep loop ticks |
| `paused_timeout_seconds` | *none* | how long an `input-required` run may wait; unset skips that sweep entirely |
| `token_signing_secret` | **required** | signs KYOK tokens |
| `identity_private_key` | *none* | souk's own Ed25519 seed; unset means souk has no identity and `sign()` raises |

Two timeouts a reader looks for here are deliberately **not** settings:
the delivery timeout on a single offer (5 s) and the unserved window
before a queued run is given up on (45 s) are `RunBroker` constructor
arguments. They describe dispatch, which an embedder can replace by
passing its own broker to `Souk(broker=...)`.

## The public URL is content, not configuration

A serving layer knows the URL callers reach it at; core does not, and
must not — naming one would make core describe a wire. So no setting
carries it, and the adapters take none: `A2AAdapter(souk)` is the whole
constructor.

Where a public URL genuinely has to appear — an agent card advertising
where to call the agent — it is passed **per call** as content:

```python
await A2AAdapter(souk).agent_card(agent, interfaces=[served])
```

Each `ServedInterface` carries its own `url` and `binding`, so one souk
can advertise the same agent over several wires, and omitting
`interfaces` omits the block from the card. The serving layer supplies
what it alone knows, once, at the point it is needed.

## Attach authentication has no switch

A link either proves its key or is refused, so the handshake is the same
on every souk. The sequencing matters as much as the rule: the proof is
verified **before** the registered-names check, so an attach that cannot
prove itself never learns whether a name is registered.

## Starting and stopping

`Souk.start()` runs once — a second call returns immediately, because
its job is to fail runs left `queued` or `running` by a previous
process, and a second pass cannot reap runs queued after the first. It
is a guard, not a permanent latch: `aclose()` clears it, so a closed
souk can be started again.

`mark_run_status` is the single funnel for every status change: it
writes through the repository and then fires `RunStatusChanged`. That is
enforced rather than asked for — a test walks the AST of every module in
the package and fails on any direct call to the repository's own
`mark_run_status`, so a new call site cannot quietly skip the
notification.

One nuance worth knowing before you build on the hook: writing a status
equal to the one already stored still fires an event. Detaching
something not attached fires nothing. Subscribers are called
synchronously, before the causing call returns, and an exception one
raises is logged and swallowed.

## Design records

Why this is shaped the way it is, and what it was shaped like first:

- [Background work is not a TaskGroup](../design-records.md#background-work-is-not-a-taskgroup)
