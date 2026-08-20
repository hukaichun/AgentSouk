# The dispatch trunk

Part of [core components](../core-components.md).

Four lanes, all network-free — each is a set of methods a serving layer
puts on whatever wire it chooses. This page describes how each lane
actually works, in order of what happens.

## The caller doors: AG-UI and A2A

Starting a run over AG-UI (`protocols/agui.py`) is a straight line:
verify the caller's metadata (an actor chain, if attached, is verified
here and a summary added), create or reopen the thread and run rows,
append the caller's messages to the thread, and check the agent is
currently served — an offline agent fails the run immediately with a
terminal event rather than queueing into silence. Then the run input is
built (below), handed to the broker, and the caller gets back a live
**event stream**: an async iterator that yields each AG-UI event as the
provider produces it — carrying **souk's** thread id, which is the
authoritative one: a caller-supplied `threadId` souk does not know is
not adopted, and the substitution rides back on every event rather than
happening silently (see
[conversation naming rights](../design-records.md#conversation-naming-rights-wait-for-a-caller-to-own-them)).
A run on a thread that already has one in flight
is accepted and queued behind it, its stream silent until its turn —
AG-UI has no "accepted, not yet worked on" state to answer with, and an
AG-UI client holds one session per thread, so the unusual second run is
queued rather than refused. Resuming a paused run is the same door with
a `resume` payload — the run keeps its id and its provider is invoked
again, targeting the thread's `input-required` run specifically. Two
callers answering the same question race, and the loser gets a
`ThreadSnapshot` of the thread as it now stands rather than a stream:
the question was already answered, so there is no second resume to
watch.

The A2A door (`protocols/a2a.py`) speaks JSON-RPC with method names read
off the A2A service descriptor (nothing hand-written, so an upstream
rename fails at import). `message/send` creates a thread and run through
the same repository calls, with one deliberate difference: an unknown
`contextId` is refused rather than replaced, because A2A's spec assigns
that id server-side while AG-UI's is client-chosen and required. Task
states are derived from run statuses; `referenceTaskIds`
records lineage; `tasks/cancel` is the one external cancel path. Every
message is kept: one whose `taskId` names the thread's paused
`input-required` task is the answer to its question and resumes that
run (status-guarded, so concurrent replies resolve to one resume); any
other message becomes a new queued run on the thread — including one
sent while a run is active, which waits its turn rather than being
merged, refused, or dropped.

## The translation: A2A becomes AG-UI before dispatch

Both doors converge on one function that builds the AG-UI
`RunAgentInput` the provider will see: thread id, run id, the folded
message history, and `forwardedProps` — the caller's free-form slot plus
souk's own two additions (`caller`, `kyok`), built by a single shared
builder so a run's identity props are byte-identical whichever protocol
dispatched it. An agent therefore becomes A2A-callable without its
author writing any A2A: by the time the run reaches the provider, the
protocol difference has already been erased.

## The agent-provider lane: offer, claim, pipeline

`RunBroker` keeps the live state in memory: a `Run` object per active
run, a pending deque per agent, and a capacity bucket per provider
(declared limit vs in-flight count). A sweep task wakes whenever work
arrives or capacity frees, and walks each agent's queue:

1. **Offer.** The earliest queued run whose thread has no run in flight
   is offered to the agent's attached connection — one awaited call
   carrying the claimed-run envelope, under a delivery timeout. Dispatch
   is **one turn per thread at a time**: a run whose thread is held by a
   claimed or paused run is passed over (without blocking other
   threads' runs behind it), which preserves per-thread order — the
   holder is by construction an earlier run on that thread. A paused
   `input-required` run keeps its thread until its question is answered
   or it is failed as stale, so no queued sibling overtakes an
   unanswered question; the gate is re-seeded from paused runs at
   startup, since dispatch state does not survive a restart but a
   paused run does. The provider answers accepted / declined-full /
   refused-permanently; timeouts and refusals are handled per
   [runs and cancels are requests](../mechanisms/requests.md).
2. **Claim.** An accepted run is marked claimed by that provider's key
   and its in-flight count rises.
3. **Pipeline.** Each claimed run gets its own consumer task draining a
   per-run command queue **in order**: the provider reporting an event
   becomes a relay command (persist the event row, forward it to the
   caller's live stream); finishing the stream folds the run's outcome
   and writes the terminal status; a cancel request is forwarded to the
   provider's `cancel` and the pipeline keeps running until a terminal
   command actually arrives. Ordering per run is guaranteed by the queue;
   runs are independent of each other.

When a run ends — however it ends — one funnel (`forget`) releases its
state: capacity is freed, the sweep wakes, KYOK bindings die, listeners
are told.

## The LLM-provider lane: the completion relay

The KYOK door (`protocols/kyok.py`) is request-scoped, no queue. A call
arrives bearing the run's token; three checks run in order — the token
verifies and hasn't expired, the run it names is still live for that
agent, and the call is freshly signed by the agent provider's own key
over the token, a timestamp and the body hash. Then the run's binding
names an offering; the offering resolves to whichever connection
currently serves it (attach/re-attach mid-run just works, because the
binding never names a connection); and the provider's chunk iterator is
returned to the caller, wrapped in a counter that records how the stream
ended. Not attached → immediate fast-fail, because the calling agent is
holding a live stream open — queueing here would help nobody.

## One deliverer, and why that is load-bearing

Exactly one place in the process offers a run to a provider: the
broker's own loop. `enqueue_run` and attaching a provider do not deliver
anything — they set a flag that wakes the loop, which then walks the
queues. The call that actually hands a run over has a single call site,
and the sweep that finds candidates has a single caller.

That is an invariant, not a coincidence of the current code. Two callers
racing to offer the same head run both plausibly succeed, and the run is
delivered twice or claimed by one provider while the other's ack arrives
against a run already in flight — either way a run is lost or
duplicated, and neither is recoverable from the outcome souk records.
Anything that needs a run dispatched sooner should wake the loop, never
deliver on its own.

The same rule explains why `enqueue_run` raises when the broker is not
running. Accepting work that nothing will ever dispatch would leave a
run `queued` forever and look, from every vantage point, exactly like a
provider that is merely busy.

## The ack, and the ack that arrives too late

A provider's answer to an offer is three-valued, so the delivery call
returns either a boolean or a refusal carrying the provider's own
reason. A truthy answer claims the run; a falsy one is a transient
decline and the run stays queued; a refusal is permanent and fails the
run with that reason recorded verbatim.

Two clocks bound the wait. A single offer has a **delivery timeout**
(5 s): expiry counts an `unanswered` against the provider and leaves the
run queued, because a provider that did not answer has not refused. An
agent left with **no serving provider** past its window (45 s) has its
queued runs failed `no_provider_took_it`, clocked from the later of when
the run was queued and when the agent went unserved. While a provider is
attached, a queued run waits indefinitely — the window times out the
absence of anyone to ask, not a provider's slowness.

A provider whose answer arrives after souk gave up can still recover the
run, and the way it does so is by behaving as though it holds it:
reporting an event for a run it does not own is read as a late ack. souk
accepts it only if the run is still unclaimed *and* the claimant is the
provider currently serving that agent, then counts an `answered_late`
and starts the pipeline. So a slow provider loses a quality counter, not
the work.

## Capacity is per identity, not per agent

The in-flight bucket is keyed by the provider's public key. One provider
serving five agents has one budget across all five, which is the same
answer souk gives everywhere else: the key is the identity, and how a
provider arranges itself behind it is its own business.

A provider that declines while claiming to have room is counted
`misdeclared` and then treated as full, because its own declaration is
the only capacity figure souk has and the decline is the more recent
fact. This is also what makes self-delegation deadlock — see
[the design record](../design-records.md#self-delegation-deadlocks-a-capacity-capped-provider).

Treating-as-full only has anything to set, though, when the provider
declared a finite limit. A provider that declared **unlimited**
concurrency and then declines is counted `misdeclared` and re-offered
immediately, every sweep, for as long as it keeps declining — the
counter records the discourtesy, but nothing backs off. Declaring no
limit and meaning it is the contract; declaring no limit and declining
is the one misdeclaration souk cannot act on.

## One substrate under both

A broker and a relay are deliberately different machines — one queues
and negotiates, one passes through — but each keeps the same roster:
a plain map from ref to connection where re-attaching under the same ref
replaces the old link (one connection per role), plus per-identity
counters. That table is extracted once as `LiveRoster` and composed by
both hosts, so the two lanes cannot drift apart; the register / attach /
detach ceremony above them is likewise stated once, in the facade's
`_Roster` base.

## Design records

Why this is shaped the way it is, and what it was shaped like first:

- [Wrapping an unknown event in `RawEvent` is quiet corruption](../design-records.md#wrapping-an-unknown-event-in-rawevent-is-quiet-corruption)
- [Liveness stopped being an inference](../design-records.md#liveness-stopped-being-an-inference)
