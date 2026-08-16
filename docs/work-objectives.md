# Work objectives

An index, not a design. Each item links to the document that argues it; this
file exists to fix the *order*, and to say for each item how we will know it
is done — which is never "the suite is green".

## Why "the suite is green" is not the bar

Every one of these was true while 210+ tests passed:

| what was true | how it was found |
|---|---|
| two `Souk`s on one database destroy each other's live runs, silently | three processes and a hand-rolled load balancer |
| `Souk.enqueue_run` is annotated `-> RunSnapshot` and returns a live `Run` | `inspect` at runtime |
| de-listing does not remove ownership, contradicting the three functions that treat a de-listed agent as gone | a test written on the opposite assumption, which timed out |
| `run_events`' comment says its integrity is "enforced at the application layer" — nothing enforces it | wiping the database mid-run and watching orphans be written |
| `thread_history` merges two entities to interleave them, and nothing reads the interleaving | all 11 queries filter by `kind` |

None of these is a bug a test failed to cover. They are places where the code
and the design disagreed, and the tests encoded the code. A green suite over a
wrong structure is evidence that the wrongness has been worked around, not
evidence that it is absent — so every objective below names a check that is
structural (the database refuses it) or empirical (a probe reproduces it),
and treats tests as regression protection afterwards rather than as the
argument.

## The target, as invariants

1. **Identity is natural, never minted.** An agent is `(provider key, name)`.
   souk mints no identifier that anyone outside must hold, because an
   identifier only souk can produce is one nobody else can rebuild.
2. **Registering is not deleting.** Absence from a batch means offline.
   Removing something is explicit, signed, and refused for anything with a
   conversation behind it.
3. **The database enforces what the code says is enforced.** Where a comment
   claims an invariant, either a constraint holds it or the comment is a lie.
4. **souk's live state and the database never disagree in silence.** Not one
   process's memory against the row, and not one process against another's.
5. **A worker is never left guessing.** Every answer souk gives a provider
   distinguishes "nothing to do" from "something is wrong".

## Order

The ordering rule is that no item may be built on a structure a later item
replaces. That rule costs the one thing this repo would otherwise do first —
see W6.

### W1 · Land `NothingOwned` — issue #37

Branch `claude/issue-37-nothing-owned`, written, 215 tests green on SQLite and
Postgres. Invariant 5.

First because it depends on nothing and makes a currently-silent failure
audible today. It will be edited again by W3 (its trigger is computed from
agent ids, which become names), and that is accepted: a small edit later beats
a live silent failure now.

**Done when:** a provider whose registration is gone gets an error instead of
an empty list, and its own logs say so. Already demonstrated.

### W2 · One new schema baseline

Documents: `retiring-agent-id.md`, `agent-lifecycle.md`,
`broker-horizontal-scaling.md`. Invariants 1, 3, 4.

Four schema changes are queued, and later ones undo parts of earlier ones.
Written as four migrations that is churn; written as one baseline it is a
single definition. There is precedent and a stated reason: `d363d76`
collapsed the chain once already, because souk has never been released and
"a baseline that creates a column a later revision deletes costs more in
confusion than the history is worth". Databases get recreated.

In one baseline:

- `agents` keyed `(provider_key, name)`; `agent_id` gone; `provider_key` gains
  the foreign key to `providers.public_key` that it has never had
- `thread_history` split into `runs` and `thread_messages` — `run_id` becomes
  a real primary key instead of a partial unique index working around message
  rows that share it
- `run_events.run_id` becomes a real foreign key, which is what its comment
  has always claimed
- `delisted_at` dropped — after W4 nothing writes it
- the dispatch columns (`owner_id`, `lease_expires_at`, `claimed_by`,
  `cancel_requested`) present from the start, on `runs`

**Done when:** the schema is built both ways and compared column by column,
constraint by constraint, index by index, on both backends — the check
`d363d76` used. And when the orphan-write probe fails: wiping the database
mid-run must now raise a foreign key violation where it previously wrote two
orphan `run_events` rows.

### W3 · Contracts follow the schema

Document: `retiring-agent-id.md`. Invariant 1.

`claim_work` and `attach_provider` take names; the provider port takes a name;
AG-UI and A2A address `(provider, name)`; registration stops handing ids back;
`AgentSummary` exposes the pair. `souk-agent-sdk` deletes `_handle_by_id`.

**Done when:** a probe replaces the database under a running provider, the
provider re-registers, and it is serving again **with no re-attach and no new
identifier anywhere** — the case that is impossible today. Plus the ownership
test still passing with the smallest possible edit; if it needs rewriting, the
ownership model changed too and that was not this item.

### W4 · Lifecycle

Document: `agent-lifecycle.md`. Invariant 2.

Absence marks offline; `delete_agent` is signed, refused for anything online,
attached, running, or with any thread; both signing payloads get an operation
prefix.

**Done when:** a *registration* signature, presented as a deletion, is
refused. That test is written first, against today's payload, where it
**passes** — which is what proves the hole is real.

### W5 · Stop discarding what the database says

Invariant 4. `repo.mark_run_status` runs an UPDATE and throws away the
rowcount, so souk cannot tell it updated nothing.

**Done when:** the probe that wipes the database mid-run makes souk complain,
rather than completing a run that leaves no trace. Between this and W2's
foreign key, "the database is not the one I was talking to" needs no detection
mechanism of its own — the writes fail.

### W6 · Leases and sweep ownership

Document: `broker-horizontal-scaling.md`, phase 1. Invariant 4.

**This was twice recommended as the first item and it is not.** It is the only
work here that fixes damage occurring today — a booting replica marks another
node's live runs failed, and that node never learns and keeps writing — and
the argument for doing it first was exactly that. But it puts four columns on
`thread_history`, a table W2 replaces. Building it first means building on a
structure already known to be wrong and letting a green suite say it was fine,
which is the failure this document opens with. W2 carries the columns instead.

Until then the exposure is real and worth stating: **do not run two souk
processes against one database.** That includes accidentally — a rolling
deploy, an autoscaler, a restarted container overlapping its predecessor.

**Done when:** `scripts/probes/probe_multiprocess.py` scenarios [2], [2b] and
[5] turn from BROKEN to OK, in separate OS processes, on both backends.

### W7 · Make the broker surface sayable

Document: `broker-horizontal-scaling.md`, phase 0. Invariant 4.

`enqueue_run` returns what it claims; `claim` returns `ClaimedRun`s, not live
`Run`s; `subscribe_wake`/`unsubscribe_wake` become
`wait_for_work(agent_ids, timeout)`; handlers move onto the broker via an
explicit bind, because the distributed broker creates a run's pipeline on the
claiming node, which never saw the enqueue call.

**Done when:** `scripts/probes/probe_broker_surface.py` reports no live `Run`
escaping and no `asyncio` type in the surface. Behaviour is unchanged, so the
suite proves nothing here and is not the check.

### W8–W10 · The distributed broker

Document: `broker-horizontal-scaling.md`, phases 3–5. Invariant 4.

Claim from rows; cross-node subscribe by tailing `run_events`; cancel
forwarding; then declare the `Broker` protocol — last, once a second
implementation has met it.

**Done when:** all six probe scenarios are OK across real processes, and
killing the owner mid-stream reaches a consumer on another node as a terminal
`RUN_ERROR` rather than a hang.

## Not scheduled

- **Straggler acknowledgement.** At-least-once delivery from a worker.
  Designed against the distributed broker, not before it.
- **`LISTEN/NOTIFY`.** Ship polling, measure, then decide. Dialect-gated, so
  it must never be the correctness mechanism.
- **KYOK across nodes.** Structurally a second broker; same treatment, later.
  Until then it constrains gateway deployments to session affinity for
  `/kyok/*`.
- **Multi-machine.** The one dependency is lease timestamps, which come from
  Python and therefore assume one clock. Recorded so a later attempt finds
  the constraint rather than the bug.

## Branches

| branch | contents | state |
|---|---|---|
| `claude/issue-37-nothing-owned` | W1 | code, green both backends |
| `claude/agent-identity` | W3, W4 designs | design |
| `claude/broker-horizontal-scaling-6b74a4` | W6–W10 design, `scripts/probes/` | design + probes |
| `claude/work-objectives` | this file | index |

The `thread_history` split (W2) has no document yet; its argument is in this
file's opening table and in `broker-horizontal-scaling.md`'s note that a
`run_dispatch` split was deferred with "revisit if the row keeps growing" —
which the queued work now triggers, though the split that matches the strain
is messages-versus-runs, not dispatch-versus-status. Dispatch columns must
stay on the run row: the claim transition and the status transition are one
atomic write.
