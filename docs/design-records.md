# Design records

The other four chapters say what souk does. This one says why it is
shaped that way — including the shapes it had first and stopped having.

Every entry here was argued from something that happened: a probe that
returned the wrong answer, a bug that reached a caller, a measurement
taken before a rewrite. This page *is* the record now: the `design/`
working notes it was distilled from have been removed from the tree, and
each entry links to the section it came from at
[the commit before the removal](https://github.com/hukaichun/AgentSouk/tree/d78d0638c0ec2126167240c62471651b5468d35b/design).
Those links are pinned to a commit, so they keep resolving; nothing new
will be written there.

Three kinds of record: rules that shipped and whose reasoning is easy to
undo by accident; designs settled but not built; and decisions that were
made, measured, and reversed.

## Rules that shipped

### A silent hop is priced, not compelled

A provider that forwards an actor chain without extending it produces a
chain that still verifies — it has only erased itself from the path.
souk does not force anyone to sign, because souk never decides on a
provider's behalf. Enforcement belongs to the chain's consumer, whose
policy knows the expected call graph. In KYOK that consumer controls the
money: an agent whose chain does not match gets no completions. Signing
is not compelled, it is **priced**.

See [Actor chain](mechanisms/actor-chain.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/trust-and-identity.md#actor-chains-provenance-hop-by-hop)

### The verifier chooses the freshness

Registration and deletion sign over a timestamp checked against a 60s
window, which is enough for operations that are idempotent or singular.
**Connect authentication is deliberately not in that family.** A
signature whose only liveness is a self-chosen timestamp is replayable
for the whole window by anyone who observed it, and observers are not
exotic — enterprise proxies terminate TLS on the path, which is also why
channel binding was ruled out. This exact hole shipped twice: once in
souk's own early gateway (#44), once in an integrator's transport built
from the only worked example then visible (#75). Hence the
challenge-response: the verifier contributes the nonce.

See [Identity](mechanisms/identity.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/trust-and-identity.md#opening-a-link-the-verifier-chooses-the-freshness)

### Wrapping an unknown event in `RawEvent` is quiet corruption

Measured against the installed `ag-ui-protocol`: an unknown event *type*
is rejected (the `Event` union is discriminated on an enum), an unknown
*field* is preserved (`extra='allow'`), a default dump injects
`timestamp: null` and `rawEvent: null`, and a dump with
`exclude_none=True` is byte-identical to the input. So the risk is
narrower than "reparsing rewrites things", and only the first row is a
real hazard: a provider running a newer AG-UI than souk must not have its
run broken.

`RawEvent` cannot paper over it. Its `type` is a hard-coded
`Literal[EventType.RAW]`, so wrapping an unrecognized event changes what
the caller sees from the real new event type to `RAW` — not faithful
relaying, and worse than passing the event through untouched. That
rejection is the durable part of this record: the obvious mitigation
makes souk lie about what the provider said.

What shipped is narrower. `handlers._handle_relay` validates each event
against the discriminated union and forwards `cmd.event` — the original
mapping — rather than a re-dumped model, so no `timestamp: null` is ever
injected into a caller's stream, and souk reads only the fields it
decides on (`type`, and the interrupt outcome for pause detection).

**The first row is closed (#116), by a three-way rule.** An event whose
`type` souk's pinned AG-UI knows is validated strictly, and a failure
still ends the run — as does an event with no `type` string at all: both
are malformation, not version skew. An event whose `type` is a string
souk does not recognise is relayed untouched — stored and forwarded,
never branched on — because whether to skip it is the caller's decision
(AG-UI's fail-open rule; A2A's spec likewise says implementations SHOULD
ignore unrecognized fields), never the relay's. The precedent is
protobuf's own reversal: proto3 shipped with unknown fields *dropped*
and 3.5 reversed it, because dropping silently destroyed data passing
through every intermediary that parses and re-serializes — exactly the
seat souk occupies. Unknown *enum* values and unknown *oneof* branches
are preserved through a protobuf relay too, and an unknown event type is
the same shape. The discipline that keeps this from becoming dict soup:
souk never branches on an unrecognized event's content — it may only
store and forward it; peeking is reserved for strictly-validated known
types.

See [The dispatch trunk](core-components/dispatch.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/library-architecture.md#typed-data-and-where-typing-stops)

### A provider is its key, and has no other id

Registration once carried an `sdk_client_id`, a string the client picked
for itself. Two things were measured before removing it. **Two unrelated
keypairs picking the same string were both accepted**, and the second
one's session token claimed the first one's run, received its input, and
could report events into it. And **two processes of one real identity
could not share their own work**, because the SDK mints a fresh string
per process and registration overwrites the column.

It was neither an identity nor a usable per-process label. What
genuinely needs "which connection" — delivering a cancel to a live
stream — needs no id in the protocol at all: every connection of that
provider is asked, and the one without the run ignores it.

See [Identity](mechanisms/identity.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/library-architecture.md#a-provider-is-its-key-and-has-no-other-id)

### Liveness stopped being an inference

Under the claiming model, asking for work *was* the liveness fact, and
there was deliberately no heartbeat. Nothing asks now, and souk does not
need it to: it holds the provider object, so `RunBroker.serving(agent)`
is a fact rather than a deduction. This was measured before it was
changed — with `online` still derived from `last_seen_at`, an attached
provider that had just completed a run was reported offline sixty
seconds after attaching.

See [The dispatch trunk](core-components/dispatch.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/library-architecture.md#dispatch-has-been-inverted-twice-and-the-reasons-differ)

### Silence about a verdict souk has reached is a bug

`failed` used to be recorded and never told to anyone: a provider whose
`run_stream` raised produced an HTTP 200 whose event stream closed in
0.1s having emitted nothing, which a client cannot distinguish from an
agent with nothing to say. souk now emits a terminal `RUN_ERROR` in
exactly that case, persisted as well as relayed.

It is neither a protocol deviation nor a decision on anyone's behalf:
`RUN_ERROR` is AG-UI's own terminal event, the verdict is already souk's
and already in the database, and an agent that reported its own failure
is left alone. `cancelled` still gets nothing — there is no cancelled
event to send, and the only party who would read it is the one who asked
for it.

See [Runs and cancels are requests](mechanisms/requests.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/library-architecture.md#cancelling-a-request-with-the-outcome-decided-later)

### Conversation naming rights wait for a caller to own them

The two caller doors answer an unknown thread id differently — AG-UI
mints a new thread under souk's own id, A2A raises `ThreadNotFound` —
and the asymmetry turned out to be each protocol's own grammar: A2A's
spec assigns `contextId` server-side, while AG-UI's `threadId` is
required and client-chosen. What is *not* protocol grammar is the
decision underneath, and it comes from ownership (#117).

AG-UI's client-minted `threadId` is safe in AG-UI's native habitat
because the whole loop has **one owner**: the same party runs the
frontend and the agent backend, so a client naming a thread is an owner
naming its own thing. souk breaks that premise — caller and provider
are two different owners with a relay between them. Adopting a
caller-chosen name into the agent's global thread namespace would let
one party write names into another's space: guessable, collidable, and
today a thread id still acts as a capability (the open contradiction
below), so a caller who picks `"test"` has minted a skeleton key others
can guess.

The rule that shipped: **souk mints every thread id, on both doors, and
the id in souk's reply is authoritative** — it rides on every returned
event, so the substitution is visible, not silent. The caller's naming
rights are real, but a name needs something to scope it to, and souk
has no caller identity yet (an acknowledged open front). The clean end
state, once it exists: caller-chosen ids become **caller-scoped
aliases** — your `trip-planning` and mine are two threads, collision
and hijack impossible by construction, and rule zero holds because the
alias names without authorizing. Until then, adoption into the global
namespace is refused as borrowing safety that isn't there;
`ThreadOwnershipMismatch` (a thread belongs to its agent) stays the
only cross-party guard. The same decision fixed `create_if_missing`
silently dropping `parent_thread_id`.

See [The integration contract](integration-contract.md) →
issue [#117](https://github.com/hukaichun/AgentSouk/issues/117)

## Designed, not built

### Rule zero: identifiers are never credentials

Identifiers are immutable. `thread_id` is woven into history, lineage
links and A2A task references, and can never change. Credentials must be
rotatable, revocable, replaceable. Therefore nothing whose only quality
is *being known* may authorize anything: a leaked `thread_id` would be a
permanent skeleton key with no remediation path, not even a bad one.
Under this rule `thread_id` becomes a pure name that may appear in logs,
trees and task references, because knowing it grants nothing.

Today's souk does not yet work this way — see
[Open contradictions](#open-contradictions) below.

See [Responsibility chains](mechanisms/responsibility-chains.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/responsibility-chains.md#rule-zero-identifiers-are-never-credentials)

### Anonymity means the key is unlinked, not that there is no key

Three binding tiers share one mechanism — signature verification against
a key registered at thread creation — and differ only in what the key is
bound to. An **identity**: a long-lived keypair carried down every
extend-edge. **A thread and nothing else**: the client SDK generates a
throwaway keypair per thread and registers the public half
automatically, which rotates (old key signs new), revokes, and cannot be
correlated across threads. **Nothing**: a bare standard client registers
no key and core treats the thread as public.

Presenting a key stays opt-in, so no standard client is forced to
deviate.

See [Responsibility chains](mechanisms/responsibility-chains.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/responsibility-chains.md#binding-three-tiers-one-mechanism)

### One question per delegation edge decides the whole tree

Before wiring its agent to another agent, a provider owes itself one
question: *if the sub agent gets stuck or fails, can I carry on without
it?* **Yes breaks the chain**, and that is the default — an undeclared
edge breaks it. The subtree becomes the provider's own implementation
behind its A2A opacity: its human resolves the subtree's interrupts, its
KYOK offering funds them, the subtree is invisible in the caller's
thread tree, and if the subtree fails the provider's run fails *in the
provider's name*. Suppliers are trade secrets and their failures are
your failures, both from the same declaration. **No extends the chain**,
and the escalation path stays connected.

**Bundling intervention rights, cost and visibility is what makes the
bit incorruptible.** Authority alone, an agent would claim opacity while
spending the caller's key; cost alone, it would pass the bill while
hiding the work. "The user pays but may not look" and "the provider
looks but does not pay" are both structurally unexpressible. And because
miscalibrated confidence is billed — declare absorb and fail, and the
failure lands on your run, your stall record, your invoice — the
resulting break-point topology is an honest map of every provider's
declared competence boundary.

See [Responsibility chains](mechanisms/responsibility-chains.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/responsibility-chains.md#delegation-the-edge-declaration)

### Authorization is not disclosure

souk verifies the segment head's signature, and the provider learns only
that the head resolved. Whether the provider learns *who* the head is
stays a separate, caller-controlled switch — the same two-layer split
KYOK's context relay already implements, where the context is relayed in
memory, never persisted, never volunteered. These are two switches by
design: do not print the head key to the provider as a convenience.

See [Responsibility chains](mechanisms/responsibility-chains.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/responsibility-chains.md#authorization-is-not-disclosure)

### The two lanes

Most protocol confusion about multi-turn conversation comes from
conflating two lanes. The **queue lane** carries ordinary utterances and
is state-independent: a caller may speak while the provider is working,
while a run is paused, or on a quiet thread; delivery is always
accepted, and *handling* is scheduled by the provider. The **reply lane**
carries the bound answer to a specific paused run's `input-required`
question.

`input-required` is an explicit pause marker governing **only the reply
lane**. It says nothing about whether the queue lane is open, because the
queue lane is always open.

On souk's A2A surface the reply lane's marker, until A2A v1.1's
`elicitationId` lands, is a message whose `taskId` names the thread's
paused task: that resumes it. Any other message is the queue lane.

[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/conversation-semantics.md#the-two-lanes)

### Queueing makes "may I speak?" always answerable with yes

Every caller utterance is accepted at any moment and queued as a run on
the thread, and the provider decides when or whether to claim it. Wire
behavior becomes deterministic with no "not accepting input" error, and
mid-execution steering needs no protocol flag — **an agent that drains
its queue eagerly *is* a steerable agent**. Timing is the provider's
will, not the protocol's ruling and not souk's.

souk never merges a queued message into an active run. Inferring which
messages belong together is the batch-correlation swamp that killed an
earlier design; membership is only ever declared by the caller and
absorbed by the provider, never assumed by the relay.

This shipped for both doors (before it, the A2A door answered a mid-run
message with the in-flight task and **silently discarded** the message —
not queued, not appended, invisible to the caller — and the AG-UI door
refused a busy thread with a snapshot), with the dispatch half in the
broker: one turn per thread at a time, and a paused `input-required` run
holds its thread until answered, so nothing overtakes an unanswered
question.

The queue is a **run queue, and every entry stays a run**: each is
dispatched whole, as its own turn, and never changes shape. A reading
that tempted us and was rejected — "one buffer, two consumers", where
interjection (designed below, not built) would be the provider reaching
into the same queue early — puts an entry's meaning in the hands of
whoever picks it up: the batch-correlation swamp reborn, and a run
whose work was absorbed into another turn would need a terminal status
souk never observed. So the two verbs stay separate at birth, declared
by the caller and never inferred: a queued entry is always a run;
an interjection travels through its own opt-in entry point with
`parentRunId`, is never a run at all when accepted (its response grows
out of the original stream), and only becomes one if rejected and
deliberately resent through the ordinary door. Nothing is ever dumped:
the provider receives one run per turn (whose input carries the history
folded at its arrival, with `thread_messages` as the authoritative
read).

The pending depth is bounded (`thread_queue_limit`, default 8, `None`
opts out): a live conversation holds a few unconsumed utterances, not
dozens — a depth in the tens is a runaway loop, not a caller. At the
limit the door refuses **loudly** (`ThreadQueueFull`: the message was
NOT accepted) — a resource guard, not a judgment, and honest refusal
beats accepting into a queue that will only rot. The reply lane is
never subject to it: answering the question is how the thread drains.
The count-then-create at the door is deliberately unlocked; a rare
concurrent overshoot by one is cheaper than locking a guard that is
not accounting. Two deliberate narrowings. Enqueue order is arrival order —
truly concurrent messages have no canonical order to preserve, and the
gate serializes dispatch regardless. And an AG-UI run queued behind
another holds its event stream open, silent, until its turn comes:
AG-UI has no "accepted, not yet worked on" vocabulary to answer with
(that is A2A's `submitted`), so the silence is chosen debt, guarded by
the use case — an AG-UI client holds one session per thread and rarely
opens a second run mid-turn; souk accepts the unusual one rather than
refusing it.

[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/conversation-semantics.md#queueing-delivery-is-the-protocols-timing-is-the-providers)

### Interjection: the rejection is the capability signal

An interjection payload is a plain `RunAgentInput` with a freshly minted
`runId` and a `parentRunId` pointing at the run it wants to join —
continuation and interjection differ by a single verb. A separate,
opt-in entry point answers accepted or rejected, and **on rejection the
caller resends the identical payload through the ordinary conversation
surface**, because it was a valid run request all along. Degradation is
one client-side resend, the server holds zero state for "unsupported",
and no Agent Card or handshake is needed to advertise the capability.

On absorption the response grows out of the original stream, pinned by
one reference-style marker event carrying a reference and never content,
so ownership of the message text stays with the call that submitted it.

[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/conversation-semantics.md#interjection-joining-the-run-in-flight)

## Tried, measured, reversed

### Enforcing cancellation produced a family of bugs

souk used to enforce cancellation by cancelling its own pump task and
synthesising a stream ending. That needed a started-Event handshake and
straggler absorption on the provider side, and **still deadlocked**:
cancelling a task before its first scheduling turn means its `finally`
never runs, so the run never terminated. All of it disappeared once souk
stopped deciding on the provider's behalf.

See [Runs and cancels are requests](mechanisms/requests.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/library-architecture.md#cancelling-a-request-with-the-outcome-decided-later)

### KYOK replaced two designs, both failing for one reason

**Session rendezvous**: the caller minted a session id and the token
carried it to the agent provider, which decoded its own token, connected
as the bridge, and was handed another provider's completion to answer —
probed live, with injected tool input for whatever acted on it. Hashing
the id closed the disclosure souk itself was creating, but the id
remained the entire proof.

**Single connection**: run and bridge on one duplex connection, with
correlation by construction. It died on its own correctness — the caller
cannot learn souk-minted ids early enough to present them, and any
reconnect path reintroduces "who owns this connection", the exact
question the design existed to erase.

Both failed for the same reason: **an actor with no identity**. KYOK is
now a real LLM provider on the same identity machinery.

See [Keep your own key](mechanisms/kyok.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/keep-your-own-key.md#history-two-designs-this-replaced-and-why)

### An inter-chunk timeout kills slow models and blames the wrong side

There is deliberately no timeout on a hung LLM provider. The old 30s
inter-chunk timeout killed slow models while attributing the failure to
the wrong party. A hung stream belongs to whoever is waiting — the agent
provider's own HTTP timeout, or the serving layer cancelling the relay
on disconnect.

See [Keep your own key](mechanisms/kyok.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/keep-your-own-key.md#scope--limitations-known-not-oversights)

### "Trustless" binding was rejected as false safety

Baking run and thread ids into the KYOK context looks like it would make
the binding trustless. It would not. Completion-to-run attribution and
run parenthood are souk-only records that nothing else signs, so an id in
the context still requires trusting souk for every link. souk-as-relay
trust is irreducible in this architecture, and the ids are unlearnable at
context-minting time anyway.

See [Keep your own key](mechanisms/kyok.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/keep-your-own-key.md#delegation-the-binding-follows-the-run-tree)

### Background work is not a TaskGroup

Weak-referenced tasks were silently garbage-collected, killing run
pipelines. A `TaskGroup` was rejected for the opposite reason: one
failing run must not cancel its siblings. `Souk.spawn` holds strong
references and isolates failures.

See [Support](core-components/support.md) →
[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/library-architecture.md#background-work-belongs-to-the-souk)

### Self-delegation deadlocks a capacity-capped provider

A provider with `max_concurrent_runs=1` that delegates to **its own**
agent deadlocks: the outer run holds the slot while it waits, the inner
run needs a slot from the same provider, souk offers it and the provider
declines as full, and the outer run sits `running` until the stall sweep
gives up on it — `run_stall_timeout_seconds`, 120s by default. A
provider that recurses should stay on the default unlimited capacity.
Delegating to a *different* provider is unaffected, since it has its own
budget.

souk imposes no depth limit and performs no cycle detection.

[full record](https://github.com/hukaichun/AgentSouk/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/agent-provider-guide.md#multi-agent-topologies--verified-not-just-argued)

## Open contradictions

Two things on this page disagree with each other or with the code. Both
are recorded rather than resolved, because resolving either is a design
decision nobody has made yet.

**`thread_id` is a credential today and a pure name under rule zero.**
[Rule zero](#rule-zero-identifiers-are-never-credentials) says nothing
whose only quality is being known may authorize anything. But the
current A2A surface rejects an unknown `contextId` precisely *because*
thread ids act as capability tokens in today's trust model, and the de
facto resume credential is knowledge of `thread_id` — any AG-UI call
naming the thread and carrying non-empty resume entries gets through.
Rule zero exists to replace that. Until it is built, both statements are
true of different points in time.

**AG-UI mints an unknown thread; A2A rejects an unknown context.** An
AG-UI run naming a `thread_id` that does not exist creates a new thread
(`protocols/agui.py` passes `create_if_missing=True`), while an A2A
request naming an unknown `contextId` raises `ThreadNotFound`
(`protocols/a2a.py` takes `repo.ensure_thread`'s default of `False`).
The two protocol surfaces answer the same situation in opposite ways.
Whether that asymmetry is intended has never been written down.
