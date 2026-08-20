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
provider produces it. Resuming a paused run is the same door with a
`resume` payload — the run keeps its id and its provider is invoked
again.

The A2A door (`protocols/a2a.py`) speaks JSON-RPC with method names read
off the A2A service descriptor (nothing hand-written, so an upstream
rename fails at import). `message/send` creates a thread and run the
same way; task states are derived from run statuses; `referenceTaskIds`
records lineage; `tasks/cancel` is the one external cancel path.

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

1. **Offer.** The head run is offered to the agent's attached connection
   — one awaited call carrying the claimed-run envelope, under a
   delivery timeout. The provider answers accepted / declined-full /
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
