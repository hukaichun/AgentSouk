# Design records

The other four chapters say what funduq does. This one says why it is
shaped that way — including the shapes it had first and stopped having.

Every entry here was argued from something that happened: a probe that
returned the wrong answer, a bug that reached a caller, a measurement
taken before a rewrite. This page *is* the record now: the `design/`
working notes it was distilled from have been removed from the tree, and
each entry links to the section it came from at
[the commit before the removal](https://github.com/hukaichun/funduq/tree/d78d0638c0ec2126167240c62471651b5468d35b/design).
Those links are pinned to a commit, so they keep resolving; nothing new
will be written there.

Three kinds of record: rules that shipped and whose reasoning is easy to
undo by accident; designs settled but not built; and decisions that were
made, measured, and reversed.

## Rules that shipped

### A silent hop is priced, not compelled

A provider that forwards an actor chain without extending it produces a
chain that still verifies — it has only erased itself from the path.
funduq does not force anyone to sign, because funduq never decides on a
provider's behalf. Enforcement belongs to the chain's consumer, whose
policy knows the expected call graph. In KYOK that consumer controls the
money: an agent whose chain does not match gets no completions. Signing
is not compelled, it is **priced**.

See [Actor chain](mechanisms/actor-chain.md) →
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/trust-and-identity.md#actor-chains-provenance-hop-by-hop)

### The verifier chooses the freshness

Registration and deletion sign over a timestamp checked against a 60s
window, which is enough for operations that are idempotent or singular.
**Connect authentication is deliberately not in that family.** A
signature whose only liveness is a self-chosen timestamp is replayable
for the whole window by anyone who observed it, and observers are not
exotic — enterprise proxies terminate TLS on the path, which is also why
channel binding was ruled out. This exact hole shipped twice: once in
funduq's own early gateway (#44), once in an integrator's transport built
from the only worked example then visible (#75). Hence the
challenge-response: the verifier contributes the nonce.

See [Identity](mechanisms/identity.md) →
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/trust-and-identity.md#opening-a-link-the-verifier-chooses-the-freshness)

### Wrapping an unknown event in `RawEvent` is quiet corruption

Measured against the installed `ag-ui-protocol`: an unknown event *type*
is rejected (the `Event` union is discriminated on an enum), an unknown
*field* is preserved (`extra='allow'`), a default dump injects
`timestamp: null` and `rawEvent: null`, and a dump with
`exclude_none=True` is byte-identical to the input. So the risk is
narrower than "reparsing rewrites things", and only the first row is a
real hazard: a provider running a newer AG-UI than funduq must not have its
run broken.

`RawEvent` cannot paper over it. Its `type` is a hard-coded
`Literal[EventType.RAW]`, so wrapping an unrecognized event changes what
the caller sees from the real new event type to `RAW` — not faithful
relaying, and worse than passing the event through untouched. That
rejection is the durable part of this record: the obvious mitigation
makes funduq lie about what the provider said.

What shipped is narrower. `handlers._handle_relay` validates each event
against the discriminated union and forwards `cmd.event` — the original
mapping — rather than a re-dumped model, so no `timestamp: null` is ever
injected into a caller's stream, and funduq reads only the fields it
decides on (`type`, and the interrupt outcome for pause detection).

**The first row is closed (#116), by a three-way rule.** An event whose
`type` funduq's pinned AG-UI knows is validated strictly, and a failure
still ends the run — as does an event with no `type` string at all: both
are malformation, not version skew. An event whose `type` is a string
funduq does not recognise is relayed untouched — stored and forwarded,
never branched on — because whether to skip it is the caller's decision
(AG-UI's fail-open rule; A2A's spec likewise says implementations SHOULD
ignore unrecognized fields), never the relay's. The precedent is
protobuf's own reversal: proto3 shipped with unknown fields *dropped*
and 3.5 reversed it, because dropping silently destroyed data passing
through every intermediary that parses and re-serializes — exactly the
seat funduq occupies. Unknown *enum* values and unknown *oneof* branches
are preserved through a protobuf relay too, and an unknown event type is
the same shape. The discipline that keeps this from becoming dict soup:
funduq never branches on an unrecognized event's content — it may only
store and forward it; peeking is reserved for strictly-validated known
types.

See [The dispatch trunk](core-components/dispatch.md) →
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/library-architecture.md#typed-data-and-where-typing-stops)

### What A2A cannot say lands in one named place, exhaustively

AG-UI says more than A2A can hear. Tool calls, reasoning, state patches,
step boundaries — a third of AG-UI's event types have no A2A
representation at all, because A2A's agents are black boxes by
construction and those events describe an inside.

A translator therefore has to decide what happens to them, and there is
no neutral answer available: dropping them is a disclosure decision, and
forwarding them is a disclosure decision. The official `@ag-ui/a2a`
converter drops silently (ag-ui#1938, #2091); funduq forwarded verbatim.
Neither is a bug — **both are guesses standing in for a declaration that
neither protocol has a field for.** AG-UI never needed one (one owner, so
there is no "to whom"); A2A never needed one (nothing gets out).

funduq does not make that decision either, and the reason is the
invariant: who may read what through funduq is enforced *outside*
funduq, by whoever actually holds that responsibility, and what funduq
owes them is a record they can attach to. So the rule is not a policy but
a **location**: every event with no A2A representation is carried
verbatim under one metadata key — `agui_event` on a status update,
`agui_events` on a whole task — and nowhere else.

The load-bearing property is that the seam is **exhaustive**, because a
filter can only attach to something it knows is complete. A route that
leaks by another path, or drops instead, is silently invisible to the
layer that is supposed to be deciding.
`test_every_ag_ui_event_type_is_mapped_or_reaches_the_overflow_seam`
walks `EventType` and fails when a new AG-UI type escapes both routes.

Two paths had disagreed. The live stream carried the overflow; `GetTask`
merged text deltas and dropped everything else, so **the auditing reader
saw less than the live subscriber** — backwards, for the one reader whose
whole purpose is the record. `build_task` now carries the same overflow
the stream does.

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
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/library-architecture.md#a-provider-is-its-key-and-has-no-other-id)

### Liveness stopped being an inference

Under the claiming model, asking for work *was* the liveness fact, and
there was deliberately no heartbeat. Nothing asks now, and funduq does not
need it to: it holds the provider object, so `RunBroker.serving(agent)`
is a fact rather than a deduction. This was measured before it was
changed — with `online` still derived from `last_seen_at`, an attached
provider that had just completed a run was reported offline sixty
seconds after attaching.

See [The dispatch trunk](core-components/dispatch.md) →
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/library-architecture.md#dispatch-has-been-inverted-twice-and-the-reasons-differ)

### Silence about a verdict funduq has reached is a bug

`failed` used to be recorded and never told to anyone: a provider whose
`run_stream` raised produced an HTTP 200 whose event stream closed in
0.1s having emitted nothing, which a client cannot distinguish from an
agent with nothing to say. funduq now emits a terminal `RUN_ERROR` in
exactly that case, persisted as well as relayed.

It is neither a protocol deviation nor a decision on anyone's behalf:
`RUN_ERROR` is AG-UI's own terminal event, the verdict is already funduq's
and already in the database, and an agent that reported its own failure
is left alone. `cancelled` still gets nothing — there is no cancelled
event to send, and the only party who would read it is the one who asked
for it.

The rule reaches runs the broker no longer tracks, too (#127): a stale
pause reaped as `paused_no_resume`, or an orphan reaped at startup as
`orphaned_by_funduq_restart`, has no pipeline left to push a `Fail`
through — so the same terminal `RUN_ERROR` is appended to the record
directly. There is no subscriber to relay it to (the paused round closed
normally; the orphan's subscriber died with the previous process), which
is exactly why the persisted record owes the verdict: it is the only
place a later reader can learn how the run died, and an event stream
that ends on a question while the database says `failed` is the silence
this record exists to forbid.

See [Runs and cancels are requests](mechanisms/requests.md) →
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/library-architecture.md#cancelling-a-request-with-the-outcome-decided-later)

### Conversation naming rights wait for a caller to own them

The two caller doors answer an unknown thread id differently — AG-UI
mints a new thread under funduq's own id, A2A raises `ThreadNotFound` —
and the asymmetry turned out to be each protocol's own grammar: A2A's
spec assigns `contextId` server-side, while AG-UI's `threadId` is
required and client-chosen. What is *not* protocol grammar is the
decision underneath, and it comes from ownership (#117).

AG-UI's client-minted `threadId` is safe in AG-UI's native habitat
because the whole loop has **one owner**: the same party runs the
frontend and the agent backend, so a client naming a thread is an owner
naming its own thing. funduq breaks that premise — caller and provider
are two different owners with a relay between them. Adopting a
caller-chosen name into the agent's global thread namespace would let
one party write names into another's space: guessable, collidable, and
today a thread id still acts as a capability (the open contradiction
below), so a caller who picks `"test"` has minted a skeleton key others
can guess.

The rule that shipped: **funduq mints every thread id, on both doors, and
the id in funduq's reply is authoritative** — it rides on every returned
event, so the substitution is visible, not silent. The caller's naming
rights are real, but a name needs something to scope it to, and funduq
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
issue [#117](https://github.com/hukaichun/funduq/issues/117)

### Every seam has exactly two entrances: an utterance, or a result

At the user–agent seam the inputs are a human's words or a deferred tool
call's result; at the agent–agent seam they are an agent's words or a
deferred tool call's result. The seams are symmetric — only the speaker
differs — which is why funduq's machinery never needs to know whether
the far side is human: **what needs to distinguish humans is the
responsibility layer** (who may supply a result), not the mechanism.

The deferred call is where human-in-the-loop lives. When an agent needs
a person, the protocol shape is an ask left pending (`input-required` is
that state; A2A's elicitation drafts are the same thing named) — and
"whose human may answer, and what proves it" is exactly where a
responsibility chain binds. The taxonomy sorts everything this page
already records:

- A plain message and a declared interjection are both **utterances** —
  the interjection flag is timing intent on an utterance, not a third
  entrance.
- The reply lane delivers a **result**: it lands on the thread's pending
  ask (status-guarded, so concurrent replies resolve to one winner),
  and funduq never judges whether it satisfies the ask — the asker does.
- The buffer bound (`thread_queue_limit`) applies to utterances only,
  and the reply lane's exemption stops being a special case: **results
  drain a pending ask; they never pile new input**, so refusing one
  would strand the ask it answers.
- A **result with no pending ask is refused** (the AG-UI door returns
  the thread's state), never repackaged as an utterance. This clause
  was earned by a probe: a `resume` payload sent at a thread with
  nothing paused used to slip in as a fresh run, handing the agent an
  answer to a question it never asked. Over A2A the case cannot arise —
  a result there is a plain message plus addressing, so when it lands
  on no ask it simply *is* an utterance, and degrading it is honest.
- **Cancel is outside the taxonomy** by intent: it is a control request
  about a run, not input to anyone's reasoning, and must not be cited
  as precedent for a third entrance.

Current granularity: one pending ask per run, addressed by the run's
own id. If asks ever need to be several-at-once (the shape A2A's
elicitation draft sketches), results address individual ask ids — a
widening of the result lane, not a new entrance.

The taxonomy is also the razor for reading upstream proposals: anything
that would create a third entrance is suspect. So far nothing does —
elicitations are deferred calls, timeline mid-task messages are
utterances.

### Interjection is a declared intent, and its target's agent is the judge

An interjection is a run that **asks to join another run's turn already in
flight**. Two things about that sentence are load-bearing and each was
learned the hard way.

**It is declared, never inferred.** Continuation ("this follows that, next
turn" — AG-UI's own `parentRunId`, relayed untouched) and interjection
("this wants *into* that turn now") are different verbs, and the target's
liveness must not be used to guess which one the caller meant — a
continuation sent while its target happens to still be running is not an
interjection. The caller states the intent explicitly: over A2A via the
interjection extension (A2A's own extension convention — the target's id in
the message's `metadata` under
`https://github.com/hukaichun/funduq/ext/interjection/v1/addressedRunId`;
v1.0's only mid-task verb is `CancelTask`, the v1.1 drafts cover only
*solicited* input, and this key yields to whatever official carrier lands),
over AG-UI by writing `forwardedProps.addressedRunId` directly. funduq
copies the declaration into `forwardedProps.addressedRunId` and does
nothing else with it.

**Its target's agent is the judge.** Whether the named run still has a turn
to join is a fact only the party running it holds; a relay-side answer is a
stale copy by construction — funduq#136 was exactly that staleness caught on
the wire, and the fix-of-the-fix kept shrinking until the honest shape
appeared: funduq holds *no* opinion. The agent compares one string against
its own in-flight loop: still open → absorb if it chooses; already ended →
an ordinary next turn. Degradation needs no machinery, no capability
signal, and no re-offer protocol. (The previous shape — an envelope
annotation, a broker in-flight predicate, a one-shot decline flag, and
strip-on-fallback — went down with the thread gate; see
[the reversal record](#the-thread-gate-is-retired-funduq-does-not-pace-a-providers-conversation).)

An agent that absorbs can mark the absorption point in its own stream with
an event of its own naming — the unknown-event rule (store and forward,
never branch) relays it verbatim, which is exactly the provider-authored
timestamp a coherent cross-party history needs and the relay could never
honestly write itself.

## Designed, not built

### Rule zero: identifiers are never credentials

Identifiers are immutable. `thread_id` is woven into history, lineage
links and A2A task references, and can never change. Credentials must be
rotatable, revocable, replaceable. Therefore nothing whose only quality
is *being known* may authorize anything: a leaked `thread_id` would be a
permanent skeleton key with no remediation path, not even a bad one.
Under this rule `thread_id` becomes a pure name that may appear in logs,
trees and task references, because knowing it grants nothing.

Today's funduq does not yet work this way — see
[Open contradictions](#open-contradictions) below.

See [Responsibility chains](mechanisms/responsibility-chains.md) →
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/responsibility-chains.md#rule-zero-identifiers-are-never-credentials)

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
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/responsibility-chains.md#binding-three-tiers-one-mechanism)

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
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/responsibility-chains.md#delegation-the-edge-declaration)

### Authorization is not disclosure

funduq verifies the segment head's signature, and the provider learns only
that the head resolved. Whether the provider learns *who* the head is
stays a separate, caller-controlled switch — the same two-layer split
KYOK's context relay already implements, where the context is relayed in
memory, never persisted, never volunteered. These are two switches by
design: do not print the head key to the provider as a convenience.

See [Responsibility chains](mechanisms/responsibility-chains.md) →
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/responsibility-chains.md#authorization-is-not-disclosure)

### The two lanes

Most protocol confusion about multi-turn conversation comes from
conflating two lanes. The **queue lane** carries ordinary utterances and
is state-independent: a caller may speak while the provider is working,
while a run is paused, or on a quiet thread; delivery is always
accepted, and *handling* is scheduled by the provider. The **reply lane**
carries an utterance addressed to a specific paused run's
`input-required` question — and "addressed to" is the whole condition:
funduq resumes the paused run with whatever the caller said and never
checks that it answers the question. "Forget the passport, book the
train instead" rides this lane as legitimately as the passport number
does; the provider reads the thread's shape — its own question is in
the history, the interrupt is in the run's metadata — and decides for
itself whether it was answered, redirected, or overruled. Whether an
utterance answers a question is a semantic judgment, and funduq does not
make those. (This is also funduq's answer to the upstream "the human
doesn't want to answer, they want to keep talking" gap: address the
paused task and say anything.)

`input-required` is an explicit pause marker governing **only the reply
lane**. It says nothing about whether the queue lane is open, because the
queue lane is always open.

On funduq's A2A surface the reply lane's marker, until A2A v1.1's
`elicitationId` lands, is a message whose `taskId` names the thread's
paused task: that resumes it. Any other message is the queue lane.

[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/conversation-semantics.md#the-two-lanes)

### Queueing makes "may I speak?" always answerable with yes

Every caller utterance is accepted at any moment and queued as a run on
the thread, and the provider decides when or whether to claim it. Wire
behavior becomes deterministic with no "not accepting input" error, and
mid-execution steering needs no protocol flag — **an agent that drains
its queue eagerly *is* a steerable agent**. Timing is the provider's
will, not the protocol's ruling and not funduq's.

funduq never merges a queued message into an active run. Inferring which
messages belong together is the batch-correlation swamp that killed an
earlier design; membership is only ever declared by the caller and
absorbed by the provider, never assumed by the relay.

This shipped for both doors (before it, the A2A door answered a mid-run
message with the in-flight task and **silently discarded** the message —
not queued, not appended, invisible to the caller — and the AG-UI door
refused a busy thread with a snapshot). The dispatch half is arrival
order and nothing else: a thread's runs are offered in the order they
came, without waiting for the previous turn to end — funduq's own
turn-pacing was tried and retired (see
[the reversal record](#the-thread-gate-is-retired-funduq-does-not-pace-a-providers-conversation)).

The queue is a **run queue, and every entry stays a run**: each is
dispatched whole and never changes shape. A reading that tempted us and
was rejected — "one buffer, two consumers", where interjection would be
the provider reaching into the same queue and *reinterpreting* an entry
— puts an entry's meaning in the hands of whoever picks it up: the
batch-correlation swamp reborn. What survives instead: an entry's verb
is declared by the caller at birth and never inferred, and an
interjection-intended utterance is *still a run*, its intent declared at
the door (see the interjection record above); the declaration changes
nothing in dispatch. Nothing is ever
dumped: absent that annotation the provider receives one run per turn
(whose input carries the history folded at its arrival, with
`thread_messages` as the authoritative read).

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
truly concurrent messages have no canonical order to preserve. And an AG-UI run arriving behind
another holds its event stream open, silent, until its provider starts
producing:
AG-UI has no "accepted, not yet worked on" vocabulary to answer with
(that is A2A's `submitted`), so the silence is chosen debt, guarded by
the use case — an AG-UI client holds one session per thread and rarely
opens a second run mid-turn; funduq accepts the unusual one rather than
refusing it.

[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/conversation-semantics.md#queueing-delivery-is-the-protocols-timing-is-the-providers)

## Tried, measured, reversed

### The thread gate is retired — funduq does not pace a provider's conversation

What shipped first (the queueing and interjection work): a broker-side
thread gate — one turn per thread at a time, a paused `input-required`
run holding its thread, and an interjection *exemption* through the gate
with the provider's ack as the negotiation.

What killed it was a probe, then a chain of diagnoses that each went one
level deeper. funduq#136 caught the exemption's annotation naming a run
that had already ended: the gate opened in `mark_run_status` a beat
before the run left the broker's tracking in `forget`, and the sweep
read the two half-updated views in between. The first fix aligned one
reader with the gate and was rightly rejected as a workaround — the
window stayed open for every other reader. The second proposal closed
the window by doing both updates in one synchronous section, and was
rejected for the better reason: correctness resting on single-event-loop
scheduling is a design that horizontal scaling must throw away.

The real defects, in the order they were found:

1. **The run-in-handler discipline never covered thread scope.** Runs
   had one owner and an ordered command queue from day one; the gate —
   thread-scope state added later — was a bare dict mutated from four
   call sites, with the sweep reading and writing outside any queue.
   A state scope added later must inherit the single-owner discipline
   on the day it is born.
2. **The status machine had no order.** `mark_run_status` was an
   unguarded UPDATE — any string, from any state, at any time. It now
   carries the legal-transition table in the UPDATE's own WHERE clause,
   so the database arbitrates racing writers and the loser's write
   matches zero rows: ordering that holds in one process or many.
3. **Deepest: the gate itself was funduq deciding when a provider drains
   its conversation** — the exact thing our own A2A #1992 comment says
   is "the provider's own decision". Every special case (the exemption,
   the one-shot flag, the annotation stripping, #136 itself) was this
   contradiction paying interest.

What replaced it: offers go head-of-queue in arrival order — per-thread
order holds by construction, and a sibling is offered while the previous
turn still runs; the SDK runtime hands every run to the agent author's
code and answers nothing on its behalf, with `serialize_per_thread` as
the one-line opt-in for authors who want turn-taking back; the buffer
bound is unchanged but now measures what it should — runs the provider
has not taken — so backpressure flows provider → buffer → loud refusal
at the door. The principle the whole episode distilled: **correctness
comes only from guarded transitions on the one ordered ledger; caches
and shadows may buy efficiency, never correctness; and funduq asserts no
fact whose owner is someone else.**

### Enforcing cancellation produced a family of bugs

funduq used to enforce cancellation by cancelling its own pump task and
synthesising a stream ending. That needed a started-Event handshake and
straggler absorption on the provider side, and **still deadlocked**:
cancelling a task before its first scheduling turn means its `finally`
never runs, so the run never terminated. All of it disappeared once funduq
stopped deciding on the provider's behalf.

See [Runs and cancels are requests](mechanisms/requests.md) →
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/library-architecture.md#cancelling-a-request-with-the-outcome-decided-later)

### KYOK replaced two designs, both failing for one reason

**Session rendezvous**: the caller minted a session id and the token
carried it to the agent provider, which decoded its own token, connected
as the bridge, and was handed another provider's completion to answer —
probed live, with injected tool input for whatever acted on it. Hashing
the id closed the disclosure funduq itself was creating, but the id
remained the entire proof.

**Single connection**: run and bridge on one duplex connection, with
correlation by construction. It died on its own correctness — the caller
cannot learn funduq-minted ids early enough to present them, and any
reconnect path reintroduces "who owns this connection", the exact
question the design existed to erase.

Both failed for the same reason: **an actor with no identity**. KYOK is
now a real LLM provider on the same identity machinery.

See [Keep your own key](mechanisms/kyok.md) →
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/keep-your-own-key.md#history-two-designs-this-replaced-and-why)

### An inter-chunk timeout kills slow models and blames the wrong side

There is deliberately no timeout on a hung LLM provider. The old 30s
inter-chunk timeout killed slow models while attributing the failure to
the wrong party. A hung stream belongs to whoever is waiting — the agent
provider's own HTTP timeout, or the serving layer cancelling the relay
on disconnect.

See [Keep your own key](mechanisms/kyok.md) →
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/keep-your-own-key.md#scope--limitations-known-not-oversights)

### "Trustless" binding was rejected as false safety

Baking run and thread ids into the KYOK context looks like it would make
the binding trustless. It would not. Completion-to-run attribution and
run parenthood are funduq-only records that nothing else signs, so an id in
the context still requires trusting funduq for every link. funduq-as-relay
trust is irreducible in this architecture, and the ids are unlearnable at
context-minting time anyway.

See [Keep your own key](mechanisms/kyok.md) →
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/keep-your-own-key.md#delegation-the-binding-follows-the-run-tree)

### Background work is not a TaskGroup

Weak-referenced tasks were silently garbage-collected, killing run
pipelines. A `TaskGroup` was rejected for the opposite reason: one
failing run must not cancel its siblings. `Funduq.spawn` holds strong
references and isolates failures.

See [Support](core-components/support.md) →
[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/library-architecture.md#background-work-belongs-to-the-funduq)

### Self-delegation deadlocks a capacity-capped provider

A provider with `max_concurrent_runs=1` that delegates to **its own**
agent deadlocks: the outer run holds the slot while it waits, the inner
run needs a slot from the same provider, and funduq — tracking that
provider's budget itself — withholds the offer rather than asking. The
outer run sits `running` until the stall sweep
gives up on it — `run_stall_timeout_seconds`, 120s by default. A
provider that recurses should stay on the default unlimited capacity.
Delegating to a *different* provider is unaffected, since it has its own
budget.

funduq imposes no depth limit and performs no cycle detection.

[full record](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/agent-provider-guide.md#multi-agent-topologies--verified-not-just-argued)

## Open contradictions

One thing on this page still disagrees with the code. It is recorded
rather than resolved, because resolving it is a design decision nobody
has made yet.

**`thread_id` is a credential today and a pure name under rule zero.**
[Rule zero](#rule-zero-identifiers-are-never-credentials) says nothing
whose only quality is being known may authorize anything. But the
current A2A surface rejects an unknown `contextId` precisely *because*
thread ids act as capability tokens in today's trust model, and the de
facto resume credential is knowledge of `thread_id` — any AG-UI call
naming the thread and carrying non-empty resume entries gets through.
Rule zero exists to replace that. Until it is built, both statements are
true of different points in time.

Answering a paused A2A task rides `taskId`, so this contradiction now
has money and authority behind it rather than just read access: knowing
a task id is enough to answer someone else's paused question. That is
the interim marker until A2A v1.1's `elicitationId` lands, and it is the
sharpest reason rule zero needs a caller identity to scope names and
rights to.

The door asymmetry that used to be recorded here as unexplained — AG-UI
minting an unknown thread while A2A refuses one — is no longer a
contradiction: it is [each protocol's own
grammar](#conversation-naming-rights-wait-for-a-caller-to-own-them),
with one deliberate rule underneath both.
