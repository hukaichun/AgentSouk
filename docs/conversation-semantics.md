# Conversation semantics: queueing, interjection, and the two lanes

> **Status: design, not implementation.** This document backs positions souk
> has taken publicly — in
> [a2aproject/A2A#1992](https://github.com/a2aproject/A2A/issues/1992)
> (multi-turn gaps),
> [a2aproject/A2A discussion #2148](https://github.com/a2aproject/A2A/discussions/2148)
> (who may answer `input-required`), and
> [ag-ui-protocol/ag-ui#2148](https://github.com/ag-ui-protocol/ag-ui/issues/2148)
> (in-flight steering) — so that every public claim has a repo artifact
> behind it. What runs today is noted inline; everything else is settled
> direction awaiting code. Companion piece:
> [`responsibility-chains.md`](responsibility-chains.md), which answers
> *who may* do any of the below; this document answers *what happens when
> they do*.

## The two lanes

Every message a caller sends travels one of two lanes, and most protocol
confusion in this area comes from conflating them:

- **The queue lane** — ordinary utterances: "here is my next thought."
  State-independent: a caller may speak while the provider is working,
  while a run is paused, or on a quiet thread. Delivery is always
  accepted; *handling* is scheduled by the provider.
- **The reply lane** — the bound answer to a specific paused run's
  `input-required` question (AG-UI resume entries; A2A's v1.1
  `elicitationId`). `input-required` is an explicit pause marker that
  governs *only this lane*; it says nothing about whether the queue lane
  is open, because the queue lane is always open.

souk's wire surfaces already keep the lanes apart: AG-UI's `ResumeEntry`
is a distinct field, and souk's A2A surface never carries a resume at
all (see `souk/pause.py` for why that is a provider decision). A2A's
`message/send` historically lacked an answer-vs-new-utterance marker;
v1.1's `elicitationId` supplies it.

## Queueing: delivery is the protocol's, timing is the provider's

**Direction (not yet implemented — today a busy thread reports its
current state instead):** every caller utterance is accepted at any
moment and queued as a run on the thread. The provider decides when — or
whether — to claim it.

- Wire behavior becomes deterministic without any "not accepting input"
  error: the answer to "may I speak?" is always yes. (A2A's `submitted`
  state already means exactly "accepted, not yet worked on.")
- Mid-execution steering needs no protocol flag: an agent that drains its
  queue eagerly *is* a steerable agent. Timing is the provider's will —
  not the protocol's ruling, and not souk's.
- **souk never merges a queued message into an active run.** Inferring
  which messages belong together is the batch-correlation swamp that
  killed an earlier design; membership is only ever declared by the
  caller (see interjection below) and absorbed by the provider — never
  assumed by the relay.
- One new worker-port piece: an advisory notice — "a new message is
  queued on your active thread" — with the same contract as the existing
  cancel notification: souk informs, the provider is free to ignore.

## Interjection: joining the run in flight

Queueing answers "handle it next." Interjection is a different verb:
"consider this *now*, inside the turn you are currently taking." Every
legitimate interjection point is a seam between the provider's execution
steps, and only the provider knows where its seams are — so souk's whole
role is to **deliver the intent and report what became of it**.

The design (matching the proposal posted on ag-ui#2148):

- **The payload is a plain `RunAgentInput`** with a freshly minted
  `runId` and `parentRunId` pointing at the active run it wants to join.
  Continuation and interjection differ by a single verb: `parentRunId`
  natively says "next turn"; interjection asks to *join the turn in
  flight* — precisely the thing the continuation chain cannot express.
- **A separate, opt-in entry point** accepts it and answers plain JSON:
  accepted (with the target run id) or rejected (unsupported / no active
  run / the run just ended). **On rejection the caller resends the
  identical payload through the ordinary conversation surface** — it was
  a valid run request all along, so degradation is one client-side
  resend and the server holds zero new state for "unsupported". The
  rejection is itself the capability signal; no Agent Card, no handshake.
- **On absorption, the response grows out of the original stream** — the
  events the provider emits after absorbing *are* the answer. One
  reference-style marker event on that stream
  (`INTERJECTION_ACCEPTED { messageId }`) pins where absorption
  happened; it carries a reference, never content, so ownership of the
  message text stays with the call that submitted it.
- Provider-side, the delivery rides the same generalized notice channel
  as cancellation: `notices = { cancel | interjection }`, polled at step
  seams. An agent that never polls simply rejects interjections and
  remains fully conformant.

## Protocol alignment

- **A2A v1.1** (`dev-1.1`: task `timeline`, `elicitations`) is building
  the record this design assumes: mid-task client messages are exactly
  what `TimelineEntry` captures, and `elicitationId` marks the reply
  lane on the wire. Boundary rule when queueing lands: a message *with*
  a `taskId` addresses that task (reply or interjection lane); a message
  *without* one becomes the next queued task in the context.
- **AG-UI** has no in-protocol interjection today; the design above was
  proposed on ag-ui#2148. Prerequisite it surfaces: a fail-open rule
  (clients MUST skip unrecognized event types) — A2A's timeline just
  adopted the same rule for the same reason.
- **task ≡ run, context ≡ thread** is souk's standing identification and
  survives v1.1: both sides' unit of work is a pausable, resumable,
  multi-round thing that ends, inside a conversation container that
  doesn't.

## Who may do any of this

Enqueueing on a bound thread, interjecting into a run, replying to an
interrupt, cancelling — all are intervention verbs, and all take their
authorization from the same place: the thread's chain and stall keys,
per [`responsibility-chains.md`](responsibility-chains.md). This
document deliberately adds no authorization machinery of its own.

## Deliberately left open

- **Non-blocking asks** (A2A v1.1's `WAITING` elicitations — asking
  without pausing): souk's AG-UI-native pause always ends the round, so
  souk currently has no way to express "asked, still working". Real gap,
  not designed here.
- **Expected-latency semantics** for long silent work — tracked upstream
  as a2aproject/A2A#1960; souk's `fail_stalled_runs` clock is the local
  symptom.
- **Wire shapes** of the interjection entry point and the queued-run
  delivery: serving-layer decisions (AgentSoukServer's `server-mode.md`
  is the spec of record for endpoints; core ships the enqueue mechanism
  and the notice channel only).
