# Direction: the broker finds the provider, hands it the run, and takes an ack

Status: **built.** The five open questions at the bottom are answered here,
except the last one, which is the horizontal-scaling work and is where the
answer to the fourth now points.

## The instruction

> broker 要找到對應的 provider，塞給他，並且從他手上拿到 ack.

The broker locates the provider serving a run's agent, hands the run to it,
and receives an acknowledgement back. Dispatch is initiated by souk rather
than by the provider asking for work.

## What that changes

Today nothing is dispatched. `Souk.start_run` persists the run and calls
`broker.enqueue_run`, which does two things: puts the run in
`_pending_by_agent[agent]`, and sets any `asyncio.Event` a `claim_work` call
is waiting on. The run then sits there until a worker calls `claim_work` and
takes it. souk never contacts a provider.

Under this direction the broker would instead hold a way to reach each
provider, choose one, deliver the run, and wait for confirmation that it
arrived.

## What the tree already records against it

This is not a gap; it is a reversal, and the reasons the current shape exists
were measured rather than argued. They are in `docs/library-architecture.md`,
"The provider should be a worker, not something core calls", and are repeated
here so the decision is made against them rather than around them.

**The routing tables.** Under the pull model a single event crossed three
queues and two routing tables — a transport-side table keyed by run_id, a
`_pump` task turning a push into a pull, and then the broker's own table.
Inverting it left two queues and one table, and the doc is explicit that the
second table was "a property of the *pull model*", not of the wire.

**Backpressure.** A remote provider could say `max_claim=2`; an in-process one
had no equivalent, and attaching a provider then starting five runs started
all five at once — measured. Claiming is what made capacity expressible,
because only the provider knows it.

**Liveness.** "Claiming marks it seen" is the whole of souk's liveness model.
There is no heartbeat, deliberately: an in-process one existed and was removed
as "a second mechanism for a fact the claim loop already produces". If souk
pushes, that fact stops being produced and something has to replace it.

**Connection affinity.** `docs/broker-horizontal-scaling.md` records that the
obstacle to running several souk processes *used to be* stated as connection
affinity — a provider's stream is pinned to one process, so a run created on
node A cannot be dispatched by node B. Inverting the provider removed it:
"A worker is not dispatched *to*; it comes and claims, over a plain call that
any node can answer." Pushing brings that obstacle back, and it is the
obstacle the horizontal-scaling work exists to clear.

**Cancellation deadlocks.** The earlier push shape needed a started-Event
handshake and straggler absorption, and still deadlocked — cancelling a task
before its first scheduling turn meant its `finally` never ran and the run
never terminated. "All of it disappeared once souk stopped deciding on the
provider's behalf."

## What the tree already records *for* an ack

Narrower than the instruction, and worth separating out because it is the one
part that is already wanted.

souk removed a completion `ack` because it arrived after the agent had
produced and discarded its events, so the only possible response was a log
line. The doc then says the *inverted* model makes an acknowledgement worth
having again — but in the other direction: **souk acknowledging events a
worker reported**, because "in a push model the worker still holds what it
sent, so a confirmation is something it can act on — retry, or don't advance
its cursor. At-least-once delivery becomes expressible." That is listed as
"still not built".

So "take an ack from the provider" and "give the provider an ack" are two
different features. The second is already on the books and does not require
reversing dispatch.

## What was built, and what each objection turned into

**The routing tables came back — one of them.** A run's events still cross the
broker's own table; what did not come back is the transport-side table keyed by
run_id, because the provider is handed a run and reports against it directly.
`ConnectedProvider` is three members — `public_key`, `deliver`, `cancel` — and
that is the whole of what souk knows about anybody.

**Backpressure is declared and then corrected by being refused.** A provider
states `max_concurrent_runs`; souk keeps a bucket that size and offers nothing
once it is empty, refilling from the run terminations it already observes.
A provider that declines while souk believed it had room is recorded as
`misdeclared` and treated as full from then on — souk believes the provider,
which is the one that knows, and records that it had to find out by being
refused. The in-process case has no shortcut: `ProviderRuntime` declines by
returning False from `deliver`, exactly as a remote one would.

**Liveness stopped being an inference.** "Claiming marks it seen" is gone, and
nothing replaced it with a heartbeat. souk holds the provider object now, so
`RunBroker.serving(agent)` is a fact rather than a timestamp compared against a
window. This was measured before it was changed: with `online` still derived
from `last_seen_at`, an attached provider that had just completed a run was
reported offline sixty seconds after attaching. `online_window_seconds` is
gone with the inference.

**Cancellation did not deadlock, because souk still decides nothing.** The
earlier push shape needed a started-Event handshake because souk was running
the provider's loop. It is not: `cancel` is a request the provider may ignore,
and a run that ignores it and finishes is recorded completed.

**The ack means the provider has the run.** True and the run is running, from
that moment, recorded in the same step it is handed over. Anything else —
False, an exception, silence past `deliver_timeout_seconds` — leaves it queued.
A late ack is accepted (`accept_late_ack`) and counted as `answered_late`: the
transport is ordered, so an answer after the timeout is a slow provider, not an
overtaken frame. Runs nobody ever takes are given up on from memory
(`expire_queued`), which is what replaced the `fail_unclaimed_runs` sweep.

## Open questions this direction has to answer

1. ~~**How does the broker reach a provider?**~~ `Souk.attach_provider` takes
   anything satisfying `ConnectedProvider`. No transport-shaped state: souk
   holds an object with three members and never asks what carries them.
2. ~~**How does it know capacity?**~~ Declared up front, and the drift is
   measured rather than prevented — see `misdeclared` above.
3. ~~**What does the ack mean?**~~ That the provider has the run. See above.
4. ~~**What replaces "claiming marks it seen"?**~~ `RunBroker.serving` — and
   it is node-local, which is what makes question 5 the same question.
5. **What happens across several souk processes?** Open, and now the only
   thing left. A run created on node A for a provider attached to node B has
   to reach B, so the tree needs a shared record of which node holds which
   connection. `online` is the weaker version of the same question — "is
   *any* node serving this" — so it falls out of that record rather than
   needing its own mechanism. That record needs an expiry, because a row
   saying node B serves an agent outlives node B being killed; `last_seen_at`
   becomes that expiry, written by whoever holds the connection instead of by
   a provider asking for work.

   Every read of the provider mapping goes through `RunBroker.serving` /
   `agents_served_by` for this reason — the dict is private, so swapping the
   source is one implementation rather than a sweep through core.
