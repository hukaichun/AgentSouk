# Direction: the broker finds the provider, hands it the run, and takes an ack

Status: **recorded direction, not designed and not built.**

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

## Open questions this direction has to answer

1. **How does the broker reach a provider?** Holding a way to contact one is
   a live connection or a callback, which is what `attach_provider` used to
   be and what put transport-shaped state in core. Where does it live now
   that core is network-free?
2. **How does it know capacity?** Only the provider knows how much it can
   take. Either it declares a number up front (which drifts) or souk asks,
   which is a round trip before every dispatch.
3. **What does the ack mean?** That the frame arrived, that the run started,
   or that it finished? Each implies a different failure mode when it does
   not come.
4. **What replaces "claiming marks it seen"?**
5. **What happens across several souk processes?** A run created on a node
   that cannot reach the provider has to hand off to one that can, which is
   the affinity problem the current shape does not have.
