# Broker horizontal scaling: design

Status: **design, not yet implemented.** This picks up where
`docs/library-architecture.md`'s "What this leaves open: horizontal scaling"
stops. That section stays the record of *why* the door was left open and what
two replicas do today; this document is the plan for walking through it.

## Goal

N souk processes against one Postgres, behind a plain load balancer:

- a run created on node A is claimable by a worker whose connection landed
  on node B;
- a consumer streaming on node A receives everything that worker reports to
  node B;
- a cancel requested on any node reaches the worker, wherever its connection
  is;
- sweeps and startup reconciliation never reap another node's live runs;
- a node dying mid-run is detected, and its runs end as `failed` with a real
  terminal event to whoever is watching — the same account a stalled
  provider already gets.

## Non-goals

- **Provider-side scaling.** Already settled in `library-architecture.md`:
  how a provider divides work between its own processes is its business, and
  souk will not move a run between worker instances.
- **SQLite multi-node.** SQLite stays what `config.py` says it is:
  single-node. The distributed broker must still *run* on SQLite — the suite
  runs there, and `souk-db-dialect-neutral` is a standing constraint — but
  nobody deploys two processes against one SQLite file, and no design
  decision here is allowed to depend on doing so.
- **New infrastructure.** v1 uses the database and nothing else. A pub/sub
  bus is one of the three relay options the architecture doc lists; it is
  rejected for v1 because core knows a database and nothing else, and a bus
  would be a second thing core has to know. Postgres LISTEN/NOTIFY is
  discussed below as a latency optimization, not a correctness dependency.
- **At-least-once event delivery.** The reconnect-straggler problem gets a
  door (see "Non-owned pushes"), but the acknowledgement the architecture
  doc calls "worth having" remains its own design.

## Measured first, on two Souks against one database

Before designing anything, a throwaway probe stood up two `Souk` instances
on one SQLite file (the dialect is irrelevant to every line below — none of
this is a locking or SQL question) and walked what a load balancer would do
by accident. Every claim the architecture doc's table makes held, and two
things it does not say showed up:

```
A enqueued run run_33653ade…            status: queued
B claim_work            -> 0 run(s)     A's run is invisible to B
A claim_work            -> 1 run(s)
  status after A claim:    queued       ← not 'running'
B start()               -> reaped ['run_33653ade…']
  status after B start:    failed  {'failureReason': 'orphaned_by_souk_restart'}
  A still dispatching it:  True
B report_event          -> False        silently dropped, worker never learns
  status 0.5s later:       failed       the verdict stands
A report_event          -> True, persisted events: 1
```

- **A keeps writing into a run the database says failed.** The doc records
  that B marks A's live runs failed; what it does not say is that A does not
  find out and does not stop. It goes on persisting `run_events` rows and
  relaying to its consumer, against a `run_status` row reading `failed` with
  a `failureReason` naming a restart that did not happen. A caller polling
  `get_run` and a caller on the stream get contradictory accounts of the same
  run, indefinitely.
- **`claim_work` returns before the run is `running`.** The status write is
  `_handle_claim`'s, and that runs on the pipeline task, so between the two
  there is a window in which the run has been handed to a worker and the
  database still says `queued`. In-process this is invisible (nothing else
  reads the row that fast); it is exactly the window that makes another node
  reap a run that was already claimed, and it is why claiming and marking
  `running` become one transaction below.

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
is what closes the measured `queued`-after-claim window above, the one
another node's sweep reads as an unclaimed run.

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
  healthy node's runs get reaped.
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
share one value or a token minted by A fails verification on B; the docs
for the setting should say so once this ships.

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

Each phase lands green on SQLite and Postgres, per CLAUDE.md.

1. **Leases and sweep ownership.** `instance_id`; the four columns; in-memory
   broker stamps them too (cheap, and it makes phase 1 testable without the
   new broker); sweeps become conditional/idempotent; lease sweep replaces
   `fail_orphaned_runs`. **This is the only phase fixing damage that already
   occurs** — the doc's "third replica booting marks A's live runs failed" —
   and it is useful even to people who never deploy a second node on
   purpose, because load balancers and orchestrators create second nodes by
   accident.
2. **Persist claim facts.** `claimed_by` / `cancel_requested` written in the
   claim and cancel transactions; `RunBroker.claim` and `request_cancel`
   become `async` on the broker surface (`claim_work` is already async;
   `cancel_run` / `RunHandle.cancel` change signature).
3. **`DatabaseRunBroker`, claim side.** Claim-from-rows, the wake poll, the
   pipeline created at claim time on the claiming node, resume (`reopen_run`
   already writes `queued` — cross-node resume falls out). Two-process probe:
   enqueue on A, claim and complete on B.
4. **Cross-node read and command paths.** Tail-based `subscribe`, status
   watching, cancel forwarding, the non-owned-push outbox. Probe: consumer
   on A, worker on B; kill B mid-run and watch A's consumer get the lease
   sweep's RUN_ERROR.
5. **Declare the `Broker` protocol** — now that a second implementation has
   met it, which is the architecture doc's own criterion for writing one —
   and rewrite `library-architecture.md`'s horizontal-scaling section to
   point here as the record.

## How this gets verified

Per CLAUDE.md, by running something, and the something is two real
processes:

- A probe script that stands up two `Souk` instances **in separate OS
  processes** on one throwaway Postgres, with an in-process provider
  attached to one and a caller on the other, and walks: claim across nodes;
  events across nodes; cancel across nodes; `SIGKILL` the owner mid-run and
  time the lease sweep's verdict. Every line of the doc's measured
  two-replica failure table becomes an assertion with the opposite outcome.
- The suite additions run the `DatabaseRunBroker` on SQLite single-process
  (the mechanics — claim atomicity, tail termination, idempotent sweeps
  under two Souk objects in one process) and on Postgres for the dialect
  half, same as everything else.
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
