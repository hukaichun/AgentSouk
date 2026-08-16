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

Two rules decide it:

1. **No item may be built on a structure a later item replaces.** That costs
   the one thing this repo would otherwise do first — see W5.
2. **Constraints come before the work they protect.** An item that makes the
   database refuse something goes ahead of the items whose mistakes it would
   refuse. Otherwise every risky change is made under a database that accepts
   orphans silently, and the constraint arrives in time to protect nothing.

Rule 2 is why W2 is the split and nothing else. An earlier version of this
file made W2 "one new schema baseline" holding all four schema changes at
once, which was wrong in a way worth recording: it optimised the *migration
artifact* rather than the *work*. Those are separate questions. souk is
unreleased, so the baseline is regenerated from `schema.py` no matter how many
steps produced it — bundling bought nothing, and cost the ability to verify
any step on its own, which is the same mistake as trusting a green suite.

### W1 · Land `NothingOwned` — issue #37

Branch `claude/issue-37-nothing-owned`, written, 215 tests green on SQLite and
Postgres. Invariant 5.

First because it depends on nothing and makes a currently-silent failure
audible today. It will be edited again by W3 (its trigger is computed from
agent ids, which become names), and that is accepted: a small edit later beats
a live silent failure now.

**Done when:** a provider whose registration is gone gets an error instead of
an empty list, and its own logs say so. Already demonstrated.

### W2 · Split `thread_history` into `runs` and `thread_messages`

No document yet — the argument is this file's opening table plus the note in
`broker-horizontal-scaling.md` that a split was deferred with "revisit if the
row keeps growing". Invariants 3 and 4.

**First of the structural work, because it is what installs the constraints
the rest of the work needs.** One table holds two entities distinguished by a
`kind` column, and the merge exists to interleave them by a shared `id` — an
ordering nothing reads. What it costs is paid on every query: half the columns
NULL per row, a `kind` predicate that silently matches the wrong rows when
forgotten (`get_run` shipped that bug), and `run_id` unable to be a key
because the messages a run introduced carry it too.

What the split makes possible, and what nothing before it can have:

- **`run_id` becomes a real primary key**, replacing a partial unique index
  that exists to work around the shared column. A2A's `Task.id` *is* `run_id`,
  so the thing every caller addresses is finally a key.
- **`run_events.run_id` becomes a real foreign key** — which its own comment
  has always claimed ("enforced at the application layer instead") and which
  nothing has ever enforced.
- **`thread_messages.run_id` can reference `runs` too**, likewise impossible
  today.

That second one is why this is first rather than last. With it, a write into a
run the database has never heard of *fails* — so the whole "was the database
replaced underneath us" question is answered structurally, by every subsequent
change, instead of needing a mechanism of its own. Every item after this one
is made under a database that catches divergence rather than one that accepts
orphans in silence.

Folded in, being the same invariant and a few lines: `repo.mark_run_status`
runs an UPDATE and discards the rowcount, so souk cannot tell it updated
nothing.

Dispatch columns stay on the run row when W5 adds them — the claim transition
and the status transition are one atomic write, which is what makes that free.
So the split is messages-versus-runs, never dispatch-versus-status.

**Done when:** the orphan-write probe changes verdict — wiping the database
mid-run must raise a foreign key violation where today it writes two orphan
`run_events` rows and completes a run that leaves no trace. And the schema
built both ways and compared column by column on both backends, the check
`d363d76` used.

### W3 · Agent identity: `(provider_key, name)`

Document: `retiring-agent-id.md`. Invariant 1.

Schema and contracts **in one item, not two.** `retiring-agent-id.md` rejects
carrying two vocabularies at once and says so explicitly — "does not become
acceptable for being temporary" — so landing the schema and the contracts as
separate merges would violate the design in the gap between them.

`agents` keyed `(provider_key, name)`, `agent_id` gone, `provider_key` gaining
the foreign key to `providers.public_key` that it has never had; `claim_work`
and `attach_provider` take names; the provider port takes a name; AG-UI and
A2A address `(provider, name)`; registration stops handing ids back;
`AgentSummary` exposes the pair; `souk-agent-sdk` deletes `_handle_by_id`.

After W2, this touches `runs.agent_id` and `threads.agent_id` once each. Done
in the other order it would edit `thread_history.agent_id` into two columns
and then move both during the split — the same columns twice.

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

### W5 · Leases and sweep ownership

Document: `broker-horizontal-scaling.md`, phase 1. Invariant 4.

**This was twice recommended as the first item and it is not.** It is the only
work here that fixes damage occurring today — a booting replica marks another
node's live runs failed, and that node never learns and keeps writing — and
the argument for doing it first was exactly that. But it puts four columns on
`thread_history`, a table W2 replaces. Building it first means building on a
structure already known to be wrong and letting a green suite say it was fine,
which is the failure this document opens with. Here, the same four columns go
onto `runs`, once.

Until then the exposure is real and worth stating: **do not run two souk
processes against one database.** That includes accidentally — a rolling
deploy, an autoscaler, a restarted container overlapping its predecessor.

**Done when:** `scripts/probes/probe_multiprocess.py` scenarios [2], [2b] and
[5] turn from BROKEN to OK, in separate OS processes, on both backends.

### W6 · Make the broker surface sayable

Document: `broker-horizontal-scaling.md`, phase 0. Invariant 4.

`enqueue_run` returns what it claims; `claim` returns `ClaimedRun`s, not live
`Run`s; `subscribe_wake`/`unsubscribe_wake` become
`wait_for_work(agent_ids, timeout)`; handlers move onto the broker via an
explicit bind, because the distributed broker creates a run's pipeline on the
claiming node, which never saw the enqueue call.

**Done when:** `scripts/probes/probe_broker_surface.py` reports no live `Run`
escaping and no `asyncio` type in the surface. Behaviour is unchanged, so the
suite proves nothing here and is not the check.

### W7–W9 · The distributed broker

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
| `claude/broker-horizontal-scaling-6b74a4` | W5–W9 design, `scripts/probes/` | design + probes |
| `claude/work-objectives` | this file | index |

The `thread_history` split (W2) has no document yet; its argument is in this
file's opening table and in `broker-horizontal-scaling.md`'s note that a
`run_dispatch` split was deferred with "revisit if the row keeps growing" —
which the queued work now triggers, though the split that matches the strain
is messages-versus-runs, not dispatch-versus-status. Dispatch columns must
stay on the run row: the claim transition and the status transition are one
atomic write.
