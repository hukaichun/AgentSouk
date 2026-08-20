# The integration contract: what each party speaks

funduq is a relay. It carries runs to agents, completions to LLM providers,
and answers back to callers — and it does not intervene in what any of
them says: it never decides on a provider's behalf, never interprets a
payload that belongs to two other parties, and never records an outcome
it has not observed. Every other promise on this page is a consequence of
that one.

The contract splits by role, because the promises differ. Callers get
standards, untouched. Providers speak standard *shapes*, and funduq opens
the doors for them — with plumbing that is funduq's own, mandatory, and
published so nobody has to read funduq's source to implement it.

| role | you speak | funduq provides | funduq-invented parts |
|---|---|---|---|
| caller (human UI) | AG-UI, any standard client | the AG-UI endpoint, threads, history | all **opt-in** |
| caller (agent-to-agent) | A2A, any standard client | the A2A endpoint, task lineage | all **opt-in** |
| agent provider | AG-UI shapes: run input in, event stream out | AG-UI **and** A2A facades, both opened by funduq | mandatory, **published as data** |
| LLM provider | OpenAI chat-completion shapes: requests in, chunks out | the OpenAI-compatible endpoint agents call | mandatory, **published as data** |

## Callers: standards, and nothing else required

The user–agent seam is AG-UI; the agent–agent seam is A2A. A standard
client of either — unmodified, no SDK of funduq's, no extra field — works.
That is a hard rule with a test behind it, and it has teeth in both
directions: when funduq seems to need a new field or endpoint, the first
question is whether the protocol already has one, and the answer has
changed designs before.

Everything funduq adds beyond the two standards is **opt-in**: binding a
run to a KYOK offering, attaching an actor chain, any metadata mechanism
funduq invents. Opting out costs nothing — the run behaves as plain
AG-UI/A2A — and opting in is always the caller's explicit act, never an
inference funduq makes.

One small carve-out keeps the record honest: the metadata keys funduq
itself writes into a run's record (`verifiedActorChain`,
`addressedRunId`, `interrupts`, `failureReason`, and `funduq`, held in
reserve) are stripped from caller-supplied metadata at the doors. A
caller cannot plant a forged verification summary — or a fake failure
reason — wearing funduq's handwriting; everything else passes through
untouched.

## Agent providers: speak AG-UI shapes, funduq opens the doors

An agent provider hosts nothing and opens no port. It connects **out** to
funduq — from a laptop, behind NAT, inside a private subnet — and its whole
protocol obligation is a shape: accept a run input that is an AG-UI
`RunAgentInput`, produce AG-UI events back. Both of funduq's caller-facing
doors are opened by funduq on the provider's behalf: the AG-UI endpoint,
and the A2A endpoint — an agent becomes A2A-callable without its author
writing a line of A2A, because funduq translates every A2A call into the
same AG-UI-shaped run input before dispatch. One shape in, two protocols
served.

Around that shape sits plumbing no external standard defines, so funduq
defines it: an Ed25519 identity, signed registration, a challenge-answer
proof when a link opens, a three-valued answer to an offered run. These
are **not opt-in** — they are how a provider exists at all — and the
promise that replaces opt-in is threefold:

1. **minimal** — funduq invents only where no standard exists, and the
   invented surface stays as small as the job allows;
2. **published as data** — every payload, model and byte a provider must
   produce or validate is exported by the provider SDK and pinned in
   [`contract-vectors.json`](contract-vectors.json), replayable in any
   language; an implementation never needs funduq's source;
3. **guarded** — tests fail funduq's own CI when an invented surface goes
   unpublished, so the contract cannot silently grow a private corner.

## LLM providers: serve OpenAI shapes, funduq exposes the endpoint

The same pattern, one seam over. An LLM provider (the party holding a
real key — see [Keep your own key](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/keep-your-own-key.md)) also connects
out and also promises only a shape: receive a completion request, stream
back OpenAI chat-completion chunks. The OpenAI-compatible endpoint that
agents call is funduq's to expose; the provider behind it is resolved per
call. Policy — serving, refusing, pricing, whose budget a run spends —
is entirely the provider's; funduq relays a structured refusal as data and
never reads it.

The plumbing is the same machinery agent providers use (identity is
identity; one keypair may be both), under the same threefold promise.

## What a standard A2A client observes

"A standard client works unmodified" is a promise about what funduq
*requires*, not a claim that every A2A concept has a funduq meaning. These
are the identifier rules and the current gaps, stated so nobody has to
discover them from behavior.

**Identifiers are funduq's to mint.** A task id is funduq's `run_id`; a
`contextId` is funduq's `thread_id`. Neither is caller-choosable. Omitting
`contextId` on the first call is the correct pattern — funduq generates
one and returns it, and the caller passes it back to continue the
thread. An unknown `contextId` is refused rather than created, because
accepting arbitrary caller-chosen ids would let any party claim a thread
it did not originate. A `contextId` belonging to a different agent is
refused for the same reason.

**`referenceTaskIds` is lineage, not continuity.** The first reference
resolves to its thread and becomes the new thread's parent, which is how
a delegated call hangs off the thread that spawned it. It does *not*
continue that conversation — for that, pass the `contextId`. One further
consequence worth knowing: the same reference is what a KYOK binding is
inherited through.

**`Message.taskId` references an existing task.** It resolves to that
task's thread, and an unknown one is a JSON-RPC `-32001`. Note it is read
from the message, not from `params`; a `params`-level `taskId` is
ignored and the call starts a fresh thread.

**A resumed run keeps its task id.** A pause does not end a task and
resuming does not mint a successor, so a stored task id stays valid
across the pause.

### Current gaps, stated plainly

These are not design positions. They are what the code does today, and a
client author needs them.

- **Addressing a paused task over A2A rides `taskId`, not
  `elicitationId`.** A message whose `taskId` names the thread's
  `input-required` task resumes it with whatever the message says —
  funduq never checks that it answers the question; a redirection or an
  overrule rides the same road, and the provider judges it from the
  thread's shape. Who may do so is gated by nothing
  more than knowing the id, the same capability-by-identifier trust
  every thread reference carries today (a recorded contradiction, not a
  position). When A2A v1.1's `elicitationId` lands, that is the marker
  this interim rule yields to.
- **A message sent while a run is active is queued behind it.** It
  becomes a new task on the thread — appended, dispatched one turn per
  thread at a time, never merged into the active run and never dropped.
  The
  [queueing record](design-records.md#queueing-makes-may-i-speak-always-answerable-with-yes)
  carries the reasoning; a paused task holds its thread, so queued
  messages wait until its question is answered.
- **The thread's pending buffer is bounded** (`thread_queue_limit`,
  default 8). At the limit a new message is refused loudly —
  `ThreadQueueFull`, meaning NOT accepted, retry after the thread
  drains — never accepted-then-expired. Answering a paused task via
  `taskId` is exempt: the reply is how the buffer drains.
- **funduq mints every thread id, on both doors — the id in funduq's reply
  is the one to continue with.** An unseen AG-UI `threadId` gets a new
  thread under funduq's own id (carried on every returned event); the id
  you sent is deliberately not adopted, so a client that keeps resending
  its own invented id gets a fresh thread each call. A2A's unknown
  `contextId` is a plain `ThreadNotFound`, per its spec's server-assigned
  ids. The asymmetry is each protocol's own grammar; the shared rule and
  its reasoning — funduq is a relay between two owners, and a caller's
  naming rights have no caller identity to scope them to yet — are in
  [the design record](design-records.md#conversation-naming-rights-wait-for-a-caller-to-own-them).
- **An offline agent looks like a failed task, not an error.** The run
  is recorded `failed` with `agent_offline`, and the task comes back
  `FAILED` with no message part.
- **Only `RunNotFound` becomes a JSON-RPC error in core.** Unknown
  thread, thread-ownership mismatch, unknown agent, invalid actor chain
  and invalid run input all escape as Python exceptions for the serving
  layer to map. Which HTTP status a caller sees is the gateway's choice,
  not core's.
- **Non-lifecycle AG-UI events ride status updates** under a funduq
  metadata key. A standard client ignores them, which means tool-call
  events are not visible over A2A.

## Where the inventions live

What the plumbing actually is — the seven signed payload families, the
link-open challenge, actor chains and what they do and do not prove — is
[Identity is an Ed25519 keypair](mechanisms/identity.md) and
[Actor chain](mechanisms/actor-chain.md). How to carry all of it over a
wire of your own is [Writing a transport](writing-a-transport.md). This
page is the contract; those are the mechanisms it obliges funduq to
publish.
