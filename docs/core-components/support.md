# Support

Part of [core components](../core-components.md).

**Change notifications** (`changes.py`) — `Souk.on_change` pushes typed
events (roster changed, LLM roster changed, run status changed) to
whoever subscribed, so a serving layer updates what it shows without
polling. Events say *that* something changed, not what to think about it.

**Health sweeps** (`health.py`) — background loops that fail what
stalled: a claimed run with no activity past the stall timeout, a queued
run nobody took within its window. Each failure is recorded with a
reason, as an observation — the sweeps never guess at outcomes, they
time out on the absence of one. `Souk.health()` is the companion
snapshot: database reachable, schema at the expected revision,
dispatching alive.

**Settings** (`config.py`) — `CoreSettings`, environment-prefixed
`SOUK_`. Every deliberate switch lives here: the database URL and
schema, timeouts and sweep intervals, the token-signing secret, souk's
identity key, and migration-stage switches such as
`require_connect_proof` (attach authentication: mechanism always on,
enforcement flipped deliberately per deployment).
