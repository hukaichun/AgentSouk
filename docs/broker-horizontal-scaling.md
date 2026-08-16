# Broker horizontal scaling: design

Status: **design, not yet implemented.** This picks up where
`docs/library-architecture.md`'s "What this leaves open: horizontal scaling"
stops. That section stays the record of *why* the door was left open and what
two replicas do today; this document is the plan for walking through it.

## Goal

**N souk processes on one host, sharing one database, behind a load
balancer.** Not a multi-machine cluster — that scope is deliberate, and what
it buys is in "Why one host" below.

- a run created on node A is claimable by a worker whose call landed on
  node B;
- a consumer streaming on node A receives everything that worker reports to
  node B;
- a cancel requested on any node reaches the worker, wherever its connection
  is;
- sweeps and startup reconciliation never reap another node's live runs;
- a node dying mid-run is detected, and its runs end as `failed` with a real
  terminal event to whoever is watching — the same account a stalled
  provider already gets.

### Why one host

Scoping to one host does not shrink the design much — the mechanism is a
shared database either way, and every decision below would be the same for
several machines. What it buys is narrower and worth naming, because each
item is a thing that would otherwise have to be *designed* rather than
assumed:

- **One clock.** Leases expire by comparing timestamps, and `repo.py` writes
  every timestamp from Python (`datetime.now(timezone.utc)`) rather than
  from SQL — deliberately, for dialect neutrality. On one host that is one
  clock and the comparison is sound. Across machines it is not, and the fix
  would be to take lease times from the database instead. So: **the lease
  columns are the one place where going multi-host later means a change**,
  and it is a small, known one. Nothing else in this design cares.
- **SQLite stops being obviously wrong.** See the non-goal below, which this
  scope changes from "no" to "measure it".
- **The probe harness is the deployment.** `scripts/probes/` runs N souk
  processes on one host with a hand-rolled load balancer in front — which,
  at this scope, is not a simulation of the target but literally it.

Nothing here forecloses several machines. It just stops that being a thing
this design claims to have thought about.

## Non-goals

- **Multi-machine.** See above. The one dependency on co-location is lease
  timestamps.
- **Provider-side scaling.** Already settled in `library-architecture.md`:
  how a provider divides work between its own processes is its business, and
  souk will not move a run between worker instances.
- **SQLite as a *supported* multi-process backend — but no longer ruled
  out.** The earlier version of this document dismissed it, reasoning that
  `config.py` positions SQLite as single-node. Several processes on one host
  sharing one file is exactly what WAL mode is for, and the probe harness
  runs three souk processes against one SQLite file today without trouble
  (see below) — so the honest position is that it is unmeasured under
  contention, not that it is wrong. The claim query is a short conditional
  UPDATE and `busy_timeout` is already set to 5s, so the question is real.
  Postgres stays the recommendation; SQLite gets measured under load in
  phase 3 rather than assumed either way.
- **New infrastructure.** v1 uses the database and nothing else. A pub/sub
  bus is one of the three relay options the architecture doc lists; it is
  rejected for v1 because core knows a database and nothing else, and a bus
  would be a second thing core has to know. Postgres LISTEN/NOTIFY is
  discussed below as a latency optimization, not a correctness dependency.
- **At-least-once event delivery.** The reconnect-straggler problem gets a
  door (see "Non-owned pushes"), but the acknowledgement the architecture
  doc calls "worth having" remains its own design.

## The baseline, measured

`scripts/probes/probe_multiprocess.py` is the harness this design is measured
against: real OS processes, one database, and a load balancer that
round-robins every single call with no affinity (see `scripts/probes/`).
Against current code, on **both SQLite and Postgres, identically** — this is
not a dialect question:

```
[1] cross-node claim
  BROKEN a worker on B can claim a run enqueued on A: B claimed 0 run(s)
[2] a new replica boots mid-run
  BROKEN a booting replica leaves another node's live run alone: C reaped 2 run(s);
         the run now reads 'failed' (orphaned_by_souk_restart) while A is still
         dispatching it: True
  BROKEN nothing lands in a run already declared failed: the run reads 'failed',
         yet A accepted a further event (True) and 1 event(s) are persisted
[3] event reported to the node that does not hold the run
  BROKEN B answered False and 0 event(s) persisted — the worker is not told
[4] consumer on the node that does not own the run
  BROKEN B's stream yielded 0 event(s) for a run producing on A
[5] the owning node dies
       (the row reads 'queued' at the moment A dies)
  BROKEN B's sweep leaves the run at 'queued'

0/6 healthy, 6 broken
```

Three of these the architecture doc already records. Three it does not:

- **[2b] The reaped node keeps writing.** The doc says a booting replica
  marks another node's live runs failed. What it does not say is that the
  owner never finds out and does not stop: it goes on persisting
  `run_events` rows and relaying them, against a `run_status` row reading
  `failed` with a `failureReason` naming a restart that did not happen. A
  caller polling the run and a caller on its stream get contradictory
  accounts, indefinitely. This is the sharpest failure of the six, and
  reading the code would not have found it.
- **[5] No sweep keys off a node being dead.** Kill the owner and every
  surviving node is blind: the stall sweep judges `last_activity_at`, which
  says the *provider* went quiet, not that the *node* is gone. Cleanup only
  ever happens at the next boot — which is also the thing that damages live
  runs. Both halves are the same missing fact, and the lease supplies it.
- **[5, aside] `claim_work` returns before the run is `running`.** The
  status write is `_handle_claim`'s, on the run's pipeline task, after the
  claim call has already returned. The window is short — an intervening IPC
  round trip is enough to miss it, which is why scenario 2 sleeps before
  measuring — but SIGKILLing the owner right after a claim lands squarely
  in it, and the row reads `queued` for a run that was handed to a worker.

  An earlier draft of this document overstated that window, calling it the
  reason a booting node reaps a claimed run. It is not: `fail_orphaned_runs`
  matches `running` too, so the reap happens either way. The window's real
  cost is the one above — a claim that no other process ever learns about —
  and it is still why claiming and marking `running` become one transaction
  below.

## The shape in one paragraph

Every run gets an **owner**: the node whose `claim_work` handed it out, holding
a **lease** it renews while the run is live. The pending queue moves from
`RunBroker._pending_by_agent` into the rows that already exist —
`status='queued'` in `thread_history` — and claiming becomes an atomic
conditional UPDATE any node can win. The run's pipeline task, its seq counter
and its round state stay exactly what they are today, on the owner node; the
persist-before-relay rule already in `handlers._handle_relay` means
`run_events` is a complete account of the stream, so a consumer on any *other*
node subscribes by tailing those rows and watching the run's status for the
end. Cancel becomes a row flag any node can set, observed and acted on by the
owner. The in-memory `RunBroker` stays the default and is untouched; the
distributed broker is a second implementation behind the same surface, and the
`Broker` protocol is declared only once that second implementation exists —
per the architecture doc's own warning that an interface written from one
implementation is how the last imagined seam came to be believed.

## Where each piece of state lands

The architecture doc's observation that `Run` mixes three separable things is
what makes this small. Sorted by where each piece must live:

| state | today | distributed |
|---|---|---|
| identity + input (`run_id`, `agent_id`, `thread_id`, `input_json`, `protocol`) | memory + DB | DB only (already there) |
| pending queue | `_pending_by_agent` | `status='queued'` rows (already there) |
| `claimed_by` (provider key) | **memory only** | column — see below |
| `cancel_requested` | **memory only** | column |
| owner + lease | — | columns, new |
| round state (`seq`, `saw_run_finished`, `pause_payload`, `round_starting_seq`) | memory | memory, owner node only — never travels |
| `cancel_notify` callable | memory | memory, owner node only (only the claimer knows how to reach itself) |
| out relay | `out_queue` | `out_queue` locally; `run_events` + status across nodes (already there) |
| long-poll wake | `asyncio.Event` | local Event, plus a bounded DB poll for cross-node enqueues |

Two of those rows are the load-bearing discoveries from reading the code
rather than the doc: `claimed_by` and `cancel_requested` exist **only in
memory** today. `report_event`'s authorization check and `claim`'s
cancelled-run filter both read them off the live `Run`. Both facts must be
persisted at the moment they are decided, or no second node can enforce
either rule.

## The interface, and what cannot currently be said in it

The architecture doc's rule stands and this design keeps it: **the `Broker`
Protocol is written last**, once a second implementation has met it, because
an interface derived from one implementation is how the previous "swap-in
seam" came to be believed rather than checked.

But "write it last" is not "don't look at it now". Looking — by running
`inspect` over the surface rather than reading it — found four places where
the current shape cannot be met by a second implementation at all, and one of
them contradicts a decision further down this document.

```
Souk.enqueue_run   annotated -> RunSnapshot
Souk.enqueue_run   actually  -> Run          live object, both queues attached
RunBroker.claim              -> list[Run]    same
RunBroker.subscribe_wake     -> asyncio.Event
all nine methods             sync
```

- **`Run` still escapes.** `library-architecture.md` states that nothing
  outside `broker.py` holds a live `Run` and that callers get a
  `RunSnapshot`. `enqueue_run` hands out the live object, mislabelled. It is
  latent rather than active — all four call sites discard the return value
  (checked across the repo, not assumed) — which is exactly why it is cheap
  to fix now and expensive to leave: a future caller that starts using it
  would be depending on an object a distributed broker cannot produce.
  `claim` returns live `Run`s too; that one stays inside `core.claim_work`,
  which converts to `ClaimedRun`, so it never escapes core — but the
  *interface* is still stated in terms of an object only one implementation
  can make.
- **The wake seam names an in-process primitive.** `subscribe_wake` returns
  an `asyncio.Event` and `core.claim_work` awaits it directly, then
  unsubscribes and re-claims by hand. A database broker *can* satisfy this
  (set the Event from its own poll task), so the seam is not fake — but the
  operation being performed is "wait until there is work for these agents,
  or this long", and saying that instead removes the four-step dance from
  core and stops the interface from naming asyncio at all.
- **The surface is sync, and must stay partly sync.** Not an oversight to
  tidy up later: `report_event` / `finish_run` reach `broker.push`
  synchronously because `worker._execute`'s `finally` calls `finish_run`
  *while unwinding a cancellation*, where an `await` is interrupted before
  it ever reaches souk and the run hangs until the stall sweep notices. So
  the Protocol will be a mixed sync/async surface. Written down here because
  a uniform-async "cleanup" would silently reintroduce a bug that has
  already been fixed once.

  What can safely become async: `claim` and `enqueue_run` (every caller is
  already in an async context) and `request_cancel` (its callers are
  ordinary request contexts; the "safe to call mid-teardown" property in its
  docstring is about it not blocking, and is preserved by the flag write
  landing before anything else). What cannot: `push`.
- **`handlers` are supplied per `enqueue_run`, and that contradicts the
  ownership decision below.** The pipeline is spawned by whoever enqueues,
  with a `HandlerMap` that caller passes in. But the design below has the
  pipeline created **at claim time, on the claiming node** — a node that
  never saw the enqueue call and therefore never received the handlers.
  Both cannot be true. Handlers belong to the broker, given once when it is
  built: `core.enqueue_run` already rebuilds the identical
  `make_handlers(self)` map on every call, so moving it is a simplification
  on its own terms, not a cost paid for a future feature.

  The wrinkle to handle rather than gloss: `Souk.__init__` accepts a broker
  from outside (`broker or RunBroker(...)`), and at that moment the `Souk`
  the handlers close over is not finished being constructed. So this needs
  an explicit binding step (`broker.bind(souk)` or handlers passed to
  `Souk`), not a constructor argument on the broker.

None of this makes the abstraction fake. After these four changes the
surface is eight methods that two implementations can genuinely both meet —
which is the point of looking now: the Protocol gets *written* last, but the
shape it will have to describe gets *fixed* first, while it is still free.

## Decisions

### The claiming node owns the run

Three candidate owners were considered:

1. **The enqueueing node** (where the caller's SSE connection is). Then every
   event a worker reports to another node must cross to the owner before it
   can be persisted or relayed — the *common* case pays the cross-node cost,
   because a load balancer makes worker-lands-on-caller's-node a coincidence.
2. **The claiming node.** The worker's connection and the run's pipeline are
   on the same node, so claim, every `report_event`, `finish_run`, pause
   detection and the reduce-on-finish are all local — today's path exactly.
   What crosses nodes is only the consumer's read (which tails rows the
   owner already writes) and the rare command from elsewhere (cancel, a
   straggler event after a reconnect).
3. **No owner — every mutation is a DB transaction any node may perform.**
   Maximal, and it dissolves the single-pipeline-per-run model that
   `broker.py`'s whole history says is load-bearing: seq assignment races,
   concurrent handlers against one run, ordering by hope. Rejected.

Option 2. It preserves every invariant the pipeline model exists for and puts
the cross-node cost on the paths that are genuinely cross-node.

Between enqueue and claim, **nobody** owns the run and nothing needs to: a
queued run is a row. The pipeline is created at claim time, on the claiming
node. A cancel arriving before any claim is an atomic
`queued → cancelled` UPDATE — the case `_pipeline` handles today with its
never-claimed RequestCancel branch becomes a row transition with no pipeline
involved.

One property falls out for free and is worth naming: **a restart stops
killing queued runs.** Today `fail_orphaned_runs` must reap them because the
pending queue is memory and nothing would ever pick them up again. As rows in
a shared queue they simply remain claimable, by any node, including the
restarted one.

### Claiming is a conditional UPDATE, dialect-neutral

```
UPDATE thread_history
   SET status='running', claimed_by=:key, owner_id=:me, lease_expires_at=:t
 WHERE run_id=:run_id AND kind='run_status' AND status='queued'
```

Rowcount 1 means this node won; 0 means someone else did (or a cancel landed
first) and the candidate is skipped, exactly like `claim`'s silent drop of a
cancelled run today. Candidates are selected first (`status='queued'`,
`agent_id IN (…)`, ordered by `created_at`, round-robined across agent_ids in
Python as now), then claimed one conditional UPDATE at a time up to
`max_claim`. Optimistic per-row claiming is deliberately chosen over
`SELECT … FOR UPDATE SKIP LOCKED`: it is atomic on both backends, so there is
one code path, and the contention it tolerates badly (many nodes fighting
over the same few rows) is not this workload — losers skip forward, and the
sweep guarantees nothing is lost either way.

Claiming and marking `running` become **one transaction**, which is the same
no-window property `RunBroker.claim` provides in memory today ("claiming is
also being claimed"), enforced by the database instead of by the absence of
an `await`. The `Claim` command survives as the pipeline's ordering marker,
but `_handle_claim`'s status write moves into the claim transaction — which
closes the measured `queued`-after-claim window, where a run has been handed
to a worker and no other process has any way to know.

The synchronous `cancel_requested` guarantee — a cancel is visible the
instant it is requested, with no queue in between — becomes a transactional
one: cancel and claim race over the same row, the database picks a winner,
and each sees the other's write. The two-reader analysis in
`Run.cancel_requested`'s docstring is satisfied by WHERE clauses instead of
by a flag set from any thread of control.

### Cross-node subscribe tails `run_events`

The three relay options from the architecture doc, decided:

- **notify-and-tail `run_events`** — chosen. The hazard the doc names (it
  puts the database on the read path `broker.py` keeps it off) is smaller
  than it looks, because the *write* path already goes through the database
  synchronously on every event: `_handle_relay` persists and commits before
  relaying, precisely so a crash cannot show a caller an event that was
  never recorded. Tailing reads back what is already being written; nothing
  new lands on the hot path's write side.
- **pub/sub bus** — rejected for v1 (new infrastructure, see non-goals).
- **route the consumer to the owning node** — rejected: souk stops being
  stateless behind a plain load balancer, which is the deployment this
  design exists to permit.

`subscribe(run_id)` in the distributed broker: if this node owns the run,
today's path (`out_queue`), byte-identical behavior. Otherwise, tail
`run_events` by `seq` from wherever the subscription starts (from 0 — the
existing subscribe-at-creation rule in `RunHandle` carries over), and watch
the run's status. END_OF_STREAM is synthesized when the status goes terminal
or `input-required`. The status watch is not optional: a run cancelled before
any claim writes **no** event at all — its only trace is the status flip — so
a tail that only watched `run_events` would hang on exactly that case.

Terminal `RUN_ERROR` synthesis needs no cross-node work: `_handle_finish` and
`_handle_fail` already persist the failure event before relaying it, so a
remote tail reads the same account a local subscriber gets. That was the
architecture doc's "reconnecting caller reads the same account" rule paying
off a second time.

Latency for a cross-node tail is the poll interval (below). A same-node
subscriber in distributed mode keeps the in-memory queue, so the common case
— caller and owner on one node, or single-node deployments that switched the
flag on by mistake — costs nothing new.

### Cancel is a flag the owner observes

`request_cancel(run_id)` on any node:

1. Try the unclaimed case first: atomic `queued → cancelled` UPDATE. Won →
   done (status change is the whole outcome; subscribers' tails see the
   terminal status).
2. Otherwise set `cancel_requested=true` on the row. If this node is the
   owner, proceed exactly as today (push RequestCancel, `cancelling` status,
   invoke `cancel_notify`). If not, the owner picks the flag up from its
   next observation cycle (poll or notify — same channel as the wake, below)
   and runs the same handler locally.

souk's contract is unchanged: it asks, it records `cancelling`, and the
outcome is decided when the stream ends. The only new latency is
cross-node observation, bounded by the poll interval — against a request
whose honest semantics are already "sometime before the stream ends".

`cancel_run` and `RunHandle.cancel` become `async` — a cross-node cancel is
a database write, and pretending it is synchronous would mean fire-and-forget
on the one operation whose delivery the caller most wants to know about.
souk is unreleased; the break is cheap now.

### Non-owned pushes: forward through the database, keep `report_event` sync

`report_event` / `finish_run` stay synchronous. Two reasons, both already in
the tree: the contract comment in `core.report_event` (a worker must never
wait on souk's persistence), and the sharper one in `worker._execute`'s
`finally` — `finish_run` runs while unwinding a cancellation, where an
`await` would be interrupted before it ever reached souk and the run would
hang until the stall sweep noticed.

In the distributed broker, a push for a run this node owns is today's path.
A push for a run it does **not** own — the straggler case: a worker's
connection dropped mid-run and its reconnect landed on a different node —
goes onto a small in-memory outbox, flushed to a `run_commands` table (or
equivalent) by a broker-owned task, and applied by the owner's pipeline in
order with everything else. The `claimed_by` the pusher asserts travels with
the command and is checked at apply time against the persisted claim — the
authorization is preserved, the enforcement point moves to where the
authoritative fact lives.

This is fire-and-forget with a wider funnel, not delivery guarantee — the
same semantics the wire has today ("it pushed and moved on"), minus the
silent black hole that two replicas currently make of it. The real fix for
stragglers is the acknowledgement design, out of scope here.

### Sweeps: leases replace geography

The architecture doc already placed this: "which runs am I responsible for"
is the broker's question, not a rule in `health.py`. Concretely:

- Each `Souk` mints an ephemeral `instance_id` at construction.
- Claiming stamps `owner_id` and `lease_expires_at`; the owner renews the
  lease on a timer (piggybacked on the health-sweep cadence) for every run
  it is dispatching. The lease asserts *the node is alive*, distinct from
  `last_activity_at`, which asserts *the provider is producing* — conflating
  them would let a chatty run keep a dead node's lease fresh or a quiet-but-
  healthy node's runs get reaped. The measured [5] is exactly this gap:
  today nothing anywhere records the first fact, so a SIGKILLed owner's runs
  are indistinguishable from runs whose provider is merely thinking.
- **This is the one place the one-host scope is load-bearing.**
  `lease_expires_at` is written by one process and compared by another, and
  `repo.py` writes every timestamp from Python rather than from SQL (a
  deliberate dialect-neutrality choice — see its module docstring). One host
  is one clock, so the comparison is sound. Across machines it would not be,
  and the fix is known and local: take lease times from the database
  (`func.now()`), which is the one piece of dialect-specific-ish SQL this
  design would then have to justify. Written down here so that a later
  multi-host attempt finds the constraint instead of the bug.
- `fail_stalled_runs` / `fail_stale_paused_runs` keep their meaning
  (provider silent, human absent — judgeable by any node from
  `last_activity_at`) but become idempotent under concurrent sweepers: the
  mark is a conditional UPDATE that only one node can win, and only the
  winner writes the terminal RUN_ERROR event and (if it is the owner or the
  run is lease-expired) closes the stream. Every-replica-reaps-every-run
  stops being double damage and becomes a harmless race for the same
  conditional write.
- **A new lease sweep replaces `fail_orphaned_runs`.** A run whose lease has
  expired has a dead or wedged owner: any node may reap it — conditional
  UPDATE to `failed`, terminal RUN_ERROR row, `failureReason:
  "owner_lease_expired"`. Startup reconciliation then does nothing special
  at all: a crashed node's `running` runs are caught by lease expiry within
  the TTL, and its `queued` runs were never owned and stay claimable. The
  one behavior regression is deliberate: post-crash cleanup happens within
  a lease TTL instead of instantly at next boot — the price of no longer
  assuming the whole database belongs to whoever boots next, which is the
  exact assumption the doc's measured failure table shows drawing blood.

An elected sweeper was considered and rejected: leader election is
infrastructure, and idempotent conditional writes get the same outcome from
plain redundancy.

### Wake and observation: poll as the one code path, NOTIFY as a bolt-on

Three things need cross-node observation: a long-polling `claim_work` on B
waking for an enqueue on A; the owner noticing `cancel_requested` set
elsewhere; a tail noticing new `run_events` rows. All three get the same
answer: the local `asyncio.Event` keeps giving instant wakes for same-node
activity, and a bounded poll (interval configurable, order of 0.5–1s)
covers cross-node.

This is the resolution of a real tension: `souk-db-dialect-neutral` forbids
Postgres-only SQL on core paths, and LISTEN/NOTIFY is Postgres-only. So
polling is the *correctness* mechanism and runs identically on both
backends; LISTEN/NOTIFY is an optional wake accelerator behind the same seam
the current `_wake_subscribers` docstring already reserves — a capability
detected off the dialect that merely fires the same events sooner, with the
poll still underneath it. It is explicitly a later, measured optimization:
v1 ships polling only, and the probe (below) is what says whether the added
latency matters at the deployment sizes souk is actually aimed at
(enterprise-internal, not planet-scale).

### Schema changes

On the `run_status` rows of `thread_history` (nullable, so message rows are
untouched):

| column | meaning |
|---|---|
| `claimed_by` | public key of the provider holding this run — the persisted twin of `Run.claimed_by` |
| `owner_id` | instance dispatching this run's pipeline |
| `lease_expires_at` | owner liveness assertion, renewed while dispatching |
| `cancel_requested` | the fact souk actually holds, now durable |

Plus a partial index for the claim query —
`(agent_id, created_at) WHERE kind='run_status' AND status='queued'` — and,
worth taking while the migration is open: a partial unique index enforcing
one active run per thread (`thread_id WHERE kind='run_status' AND status IN
(active…)`), because today's check in `get_active_run_for_thread` is
read-then-insert and two nodes can race it. Both partial-index forms work on
SQLite and Postgres, same as the existing `run_id` index.

A separate `run_dispatch` table was considered (thread_history is already
dual-purpose and this widens it further) and deferred: the claim transition
and the status transition must be one atomic write, and keeping them in one
row is what makes that free. Revisit if the row keeps growing.

New `CoreSettings`: `broker` (`"memory"` default | `"database"`),
`lease_ttl_seconds` (~60, comfortably above the sweep interval),
`cross_node_poll_interval_seconds`. Existing but newly load-bearing:
`token_signing_secret` is already mandatory with no default — replicas must
share one value or a token minted by A fails verification on B. The probe
harness hands every node the same secret for exactly this reason (see
`cluster.py`), which is the sort of thing that reads as boilerplate until a
deployment sets it per-pod; `config.py`'s comment for the field should say
so once this ships.

## How the agent-identity work changes this

`docs/retiring-agent-id.md` and `docs/agent-lifecycle.md` land on the same
claim path this document rebuilds, so the interaction is worth stating rather
than discovering. It is mostly favourable, and it fixes the sequencing
question.

**The ownership check moves inside the claim, and stops having a gap.** Today
claiming is three steps: read which ids this key owns, filter the request
against them in Python, then claim. The property that stops one provider
claiming another's work lives *between* two statements — tolerable in one
process, and exactly the shape this document spends its length worrying about
everywhere else. With `(public_key, name)`, the filter is a column on the row
being claimed:

```sql
UPDATE thread_history
   SET status='running', claimed_by=:key, owner_id=:me, lease_expires_at=:t
 WHERE kind='run_status' AND status='queued'
   AND provider_key = :key AND agent_name IN (:names)
```

One statement, atomic, and the authorization is the same `WHERE` clause that
does the claim. A separate ownership query cannot get out of step with it,
because there isn't one.

**`claimed_by` never needed changing**, which is worth noticing: the part of
this design that identifies a *provider* was already the natural key. Only the
part identifying an *agent* was a surrogate. The two halves of the same table
were built to different rules.

**#37's `NothingOwned` gets a better home.** Merging the check into the claim
loses the ability to tell "nothing queued" from "you own none of these" —
0 rows updated means both — which is precisely the ambiguity that issue is
about. So the registration lookup survives, but only on the empty path: claim
first, and ask "is this key registered for these names at all" only when
nothing came back. That makes it free in the busy case and, on an idle
long-polling worker, it runs every cycle — which is the behaviour #37 wants,
since that worker is exactly the one that needs to find out it has been
de-registered.

**The deletion guard's cross-node gap mostly dissolves.** `agent-lifecycle.md`
flags that "no active run" is read from this process's broker, so another
node's live run might be missed. With deletion refused for any agent that has
threads, and `thread_history.thread_id` being a NOT NULL foreign key — so a
run cannot exist without a thread, at the schema level — "no threads" implies
no runs anywhere, on any node. What is left is the in-process `attached` check,
which genuinely is node-local: node A cannot see that the provider is attached
on node B. That degrades into the previous item rather than into damage — B's
worker claims for a name that no longer exists and gets `NothingOwned`, loudly.

**`delisted_at` disappearing takes a column out of the hot path.** The claim
index is on queued runs and is consulted by every worker on every node; one
fewer nullable column to reason about there is small but free.

**The one real cost: the claim index gets wider.** `(agent_id, created_at)`
becomes `(provider_key, agent_name, created_at)` — a 64-hex key plus a
name, against a 30-character id. Roughly two to three times the width on the
one index this design puts on the hottest path. Irrelevant at the deployment
size souk is aimed at, and named here so that if it ever is relevant, the fix
is already identified: `providers.fingerprint` is the 16-hex form of the same
key and is already unique.

### What this means for the order of work

- **Phase 1 (leases) is independent.** It touches runs and nodes, never
  agents, and it is the only phase fixing damage that already occurs. Do it
  first regardless.
- **Phases 3 and 4 should follow the agent-identity contract change.** Phase 3
  *is* the claim query and its index. Writing it against `agent_id` means
  writing the hardest query in this design twice and rebuilding its index
  after — for no benefit, since phase 1 does not depend on it.
- **The schema changes want to be one migration, or deliberately two.** Both
  add or replace columns on `thread_history`: this document's four dispatch
  columns, `retiring-agent-id.md`'s split of `agent_id` into two. Doing them
  together is one table rebuild on SQLite instead of two.
- **`scripts/probes/probe_multiprocess.py` is on the contract.** It calls
  `start_run` and `claim_work` with agent_ids, so the contract change updates
  it — which is fine and worth saying, because that probe is the pass/fail
  gate for every phase here and must not be allowed to rot into "the version
  that still compiles".

## What this deliberately does not change

- The in-memory broker, byte for byte. Single-process embedding stays the
  default and keeps today's semantics including instant orphan reconciliation
  at start.
- The worker loop, the provider port, the three core methods and the cancel
  notification. A worker cannot tell which broker is behind `claim_work` —
  that indistinguishability is the test that the seam is real.
- The pipeline model: one task per run, handlers as the only mutators, order
  within a run absolute.
- Protocol behavior: no new fields, no new endpoints, nothing a standard
  AG-UI or A2A client would notice (`souk-no-forced-protocol-deviation`).
- KYOK. The bridge is structurally a second broker and will need the same
  treatment; it is per-process today and stays so — a KYOK completion
  round-trips through whichever node the agent's poll landed on, which works
  only with session affinity. Documented limitation, own design later.
- `on_change` stays a node-local view, as its own docstring already warns
  ("a promise souk cannot keep across a restart or a second process").
  Subscribers on other nodes see status changes via querying, not callbacks.

## Phasing

Each phase lands green on SQLite and Postgres, per CLAUDE.md, and each names
the probe scenarios it is expected to turn from BROKEN to OK. Nothing is
"done" while its scenario still fails.

0. **Make the surface sayable.** No behaviour change, no scenario flips, and
   nothing about it is speculative — every item is a thing the current code
   states wrongly today. `enqueue_run` returns a `RunSnapshot` for real (or
   nothing — no caller uses it); `claim` returns `ClaimedRun`s rather than
   live `Run`s; `subscribe_wake`/`unsubscribe_wake` collapse into
   `wait_for_work(agent_ids, timeout)`; `handlers` move off `enqueue_run`
   onto the broker via an explicit bind. Done first because each one is
   cheap while the return values are unused and the map is rebuilt anyway,
   and because phase 3 cannot be written on top of the current shapes at
   all. See "The interface" above for how each was found.
1. **Leases and sweep ownership** → fixes **[2]**, **[2b]** and **[5]**.
   `instance_id`; the four columns; the in-memory broker stamps them too
   (cheap, and it makes this phase testable before the new broker exists);
   sweeps become conditional/idempotent; the lease sweep replaces
   `fail_orphaned_runs`. **The only phase fixing damage that already
   occurs**, and useful to people who never deploy a second node on purpose
   — rolling deploys and orchestrators create a second process for you.
2. **Persist claim facts.** `claimed_by` / `cancel_requested` written in the
   claim and cancel transactions; `RunBroker.claim` and `request_cancel`
   become `async` on the broker surface (`claim_work` is already async;
   `cancel_run` / `RunHandle.cancel` change signature). No scenario flips
   here — it is the groundwork phase 3 and 4 both stand on, which is worth
   saying out loud so its lack of a visible win is not read as a problem.
3. **`DatabaseRunBroker`, claim side** → fixes **[1]**. Claim-from-rows, the
   wake poll, the pipeline created at claim time on the claiming node,
   resume (`reopen_run` already writes `queued`, so cross-node resume falls
   out). Also where SQLite-under-contention gets measured rather than
   assumed (see non-goals): N processes claiming hard against one file, and
   the answer decides whether SQLite is documented as viable here or as
   dev-only.
4. **Cross-node read and command paths** → fixes **[3]** and **[4]**.
   Tail-based `subscribe`, status watching, cancel forwarding, the
   non-owned-push outbox. The probe gains a scenario the current six do not
   cover: kill the owner mid-stream and watch a consumer on another node
   receive the lease sweep's terminal RUN_ERROR rather than hanging.
5. **Declare the `Broker` protocol** — now that a second implementation has
   met it, which is the architecture doc's own criterion for writing one —
   and rewrite `library-architecture.md`'s horizontal-scaling section to
   point here as the record.

## How this gets verified

Per CLAUDE.md, by running something. The something is built and runs today,
against current code, where it fails 6/6 — which is what makes it a check
rather than a demo:

```bash
cd souk && uv run python ../scripts/probes/probe_multiprocess.py
```

`scripts/probes/` holds three files and no dependencies beyond souk itself:

- **`node.py`** — one souk process behind a unix socket, newline-delimited
  JSON, one request per connection. The smallest possible stand-in for a
  serving layer. Every op is a plain call into `Souk` with no shortcut past
  `claim_work`'s identity checks or `report_event`'s ownership check; a
  probe that cheated on those would prove nothing about the thing being
  probed. It is deliberately crude, because every real decision it might
  make (framework, port, auth) belongs to the gateway in its own repository,
  and making one here would be this repo growing a serving layer under
  another name.
- **`cluster.py`** — spawns N nodes as real OS processes and round-robins
  calls across them. **Harsher than a real load balancer on purpose:** no
  affinity of any kind, so the call that starts a run, the worker's claim,
  each reported event and the caller's read can land on four different
  processes. Real load balancers are kinder, and kindness is what makes this
  class of bug surface in production six months late instead of here. A
  probe names a node explicitly (`node="c"`) only when *which* node is the
  scenario — "boot a fresh replica *now*, while A holds a run".
- **`probe_multiprocess.py`** — the six scenarios above. Each prints what
  happened and what should happen instead, so the same script becomes the
  pass/fail check as each phase lands rather than being thrown away.

Two things this harness already corrected in this document, both of which a
single-process probe had got wrong:

- Two `Souk` objects in one process share an event loop, so an earlier probe
  proved less than it appeared to. These are separate processes.
- `Souk.start()` runs once per process by design, so a second `souk_start`
  on a node that already started is a no-op — which quietly made the
  "booting replica reaps live runs" scenario *pass*. It needs a genuinely
  fresh process, which is why `cluster.spawn("c")` exists.

Alongside it:

- The suite additions run the `DatabaseRunBroker` in-process (claim
  atomicity, tail termination, idempotent sweeps under two `Souk` objects)
  on both backends, same as everything else.
- `docker compose up` with two gateway replicas is downstream
  (AgentSoukServer), after the library lands — the wire was touched, so per
  CLAUDE.md it gets run.

## Open questions

1. **Straggler acknowledgement.** The outbox gives late events a path, not a
   guarantee. The at-least-once design the architecture doc sketches (worker
   holds frames until confirmed) is the real fix and should be designed
   against the distributed broker, not before it.
2. **NOTIFY.** Ship polling, measure the cross-node latency at realistic
   sizes, and only then decide whether the accelerator earns its
   dialect-gated code path.
3. **Locality bias.** Should `enqueue_run` prefer waking local workers
   before remote ones get a poll tick? Free latency win, mild fairness
   cost. Defer until the probe produces numbers.
4. **KYOK across nodes.** Same shape, later; until then it constrains
   gateway deployments to affinity for `/kyok/*`.
