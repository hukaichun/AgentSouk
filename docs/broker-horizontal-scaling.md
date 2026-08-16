# Broker horizontal scaling: design

Status: **design, not implemented.**

An earlier version of this document planned this against a *pull* model,
where a provider called souk to claim work. souk delivers now (see
`docs/library-architecture.md`, "Dispatch has been inverted twice"), which
changes the central problem rather than the goal. That version is in the git
history; what is here is what still holds.

## Goal

**N souk processes on one host, sharing one database, behind a load
balancer.** Not a multi-machine cluster.

- a run created on node A reaches the provider attached to node B;
- a consumer streaming on node A receives everything that provider reports
  to node B;
- a cancel requested on any node reaches the provider, wherever it is;
- sweeps and startup reconciliation never reap another node's live runs;
- a node dying mid-run is detected, and its runs end `failed` with a real
  terminal event to whoever is watching.

### Why one host

Scoping to one host does not shrink the design — the mechanism is a shared
database either way. What it buys is that clock skew, partition tolerance and
node discovery stay out of scope: one host means one clock and a process
list, so a lease can be a timestamp rather than a consensus problem.

## Non-goals

- Multi-machine. Later, and mostly the same design plus a clock story.
- Any queue, broker or cache that is not the database souk already has.
- Provider-side scaling. A provider runs as many processes as it likes, each
  attaching with its own declared capacity.
- Making the database part of the live event-relay hot path. It is the
  durable record; dispatch stays on asyncio primitives.

## The baseline, measured

`scripts/probes/probe_multiprocess.py` is the harness: real OS processes, one
database, and a load balancer that round-robins every call with no affinity.
Against the pull-model code, on **both SQLite and Postgres identically** —
this was never a dialect question — it reported `0/6 healthy, 6 broken`.

Three of those six were already predicted. Three were not, and they are why
this is a document rather than a paragraph:

- **A reaped node keeps writing.** A booting replica marks another node's live
  runs `failed`; the owner never finds out and does not stop. It goes on
  persisting `run_events` and relaying them against a row reading `failed`
  with a `failureReason` naming a restart that did not happen. A caller
  polling and a caller streaming get contradictory accounts, indefinitely.
- **No sweep keys off a node being dead.** Kill the owner and every surviving
  node is blind: the stall sweep judges `last_activity_at`, which says the
  *provider* went quiet, not that the *node* is gone. Cleanup only happens at
  the next boot — which is also the thing that damages live runs. Both halves
  are the same missing fact.
- **A run could be handed out without any other process learning of it.** The
  status write happened on the run's own task, after the handing-out call had
  returned. Delivery closed this one: `_offer` records `claimed_by` in the
  same synchronous step as the ack, with no await in between.

**The probe still describes the pull model and has not been re-pointed.** Its
scenarios are phrased in terms of claiming, so its verdicts are about a
mechanism that no longer exists. Re-pointing it is the first task here, not
an afterthought — without it there is no baseline for the shape below.

## What delivering changed

Pushing brings back the one thing claiming had removed. Under the pull model a
provider was not dispatched *to*; it came and asked, over a plain call any node
could answer, so a run created on A was claimable from B with no routing at
all. That is gone. **A run created on node A for a provider attached to node B
has to reach node B.**

So the shared record this needs is no longer only about runs. It is about
which node holds which provider connection.

## The shape

Two facts move from memory into the database:

1. **Which node serves which agent.** Written when a provider attaches, held
   under a lease its node renews, removed when it detaches. This is
   `RunBroker._providers`, made shared.
2. **Which node owns a live run.** The node that delivered it, under the same
   lease. A run's events, its cancel and its terminal status all have to reach
   that node.

Given (1), dispatch on node A becomes: find the node serving this agent; if it
is A, deliver locally as now; if it is B, hand the run to B. Given (2),
everything else — cross-node streaming, cancel, sweeps — is the existing
per-run routing with one extra hop.

**`online` falls out of (1) for free**, which is why (1) is worth building
first. "Is any node serving this agent" is strictly weaker than "which node
serves it", and the roster only asks the weaker one.

### The seam is already in place

Every read of the provider mapping goes through `RunBroker.serving` or
`agents_served_by`, and the mapping itself is private. That was done
deliberately when reachability replaced the `last_seen_at` inference: swapping
the source is one implementation, not a sweep through core.

### Leases replace geography

A lease is a node id plus a renewed timestamp. It answers the question all six
broken scenarios turn on — *is that node still there* — which nothing
currently answers at all. Startup reconciliation stops meaning "fail
everything that looks live" and starts meaning "fail what an expired lease
owns", which is the difference between the boot-time repair and the boot-time
damage.

`last_seen_at` becomes the expiry for (1), written by the node holding the
connection rather than by a provider asking for work — the same column doing
an honest version of its old job.

### Cross-node subscribe tails `run_events`

A consumer on a node that does not own the run reads the run's persisted
events rather than its in-memory queue. This survives from the earlier design
unchanged: `run_events` is append-only with a per-run sequence, so tailing it
is a bounded query, and `run_id` is a real foreign key now.

Polling is the one code path. Postgres `LISTEN`/`NOTIFY` is a latency
optimisation on top, never a correctness requirement — SQLite has nothing
equivalent, and one path that works on both is worth more than two that
diverge.

## What this deliberately does not change

- The core/serving boundary. None of this names a protocol or a transport.
- `ConnectedProvider`. A gateway wraps a connection; whether that connection's
  node is the one dispatching is not the provider's concern.
- Dialect neutrality. Everything above is expressible in SQLAlchemy Core on
  both backends; anything that is not gets dropped rather than special-cased.

## How this gets verified

`probe_multiprocess.py`, re-pointed at delivery, run on both backends. Its
`0/6` is the target, and it is excluded from CI precisely so it can fail
honestly until it is not.

The scenarios needing rewriting rather than adapting are the ones phrased as
claims: *a worker on B takes a run enqueued on A* becomes *a provider attached
to B receives a run created on A*.

## Open questions

1. **How does a run reach the node that can deliver it?** A row that node
   polls, or something it is notified about. The first works on both backends
   and is the default; the second is the optimisation.
2. **What happens to a run mid-delivery when its node's lease expires?** The
   ack either arrived or did not, and only the dead node knew. This is the
   same at-least-once question the delivery ack raises locally, one level up.
3. **Does the roster need to distinguish "no node serves this" from "the node
   that does is unreachable from here"?** Callers see one `online` flag; a
   partitioned answer may need two.
