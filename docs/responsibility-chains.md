# Responsibility chains: who may act on a paused thread

> **Status: design, not implementation.** Everything under "The floor we
> stand on" is live code with file references; everything after it is a
> decided direction that no code enforces yet. When implementation lands,
> each section here should either match the code or be amended
> deliberately — the same contract `library-architecture.md` holds.

## The problem, in one story

A user talks to a main agent over AG-UI. The main agent delegates part of
the job to a sub agent over A2A. The sub agent cannot proceed without a
human answer — it pauses with `input-required`.

Now every hard question fires at once. The main agent can *see* the state
(A2A translates it) but structurally cannot resume it. The user upstream
may not even know the sub thread exists. The sub agent's own operator — a
human, too; both ends of every thread in a souk have one — may be the
right person to answer, or may be exactly the wrong one. And whoever does
answer: what proves they were entitled to, and what records that it was
them?

souk already contains the mechanisms this story needs. What it did not
contain, until this document, was the *concept* — the mechanisms existed
as conveniences nobody had named, which is indistinguishable from
accidents nobody has threat-modeled. An unnamed affordance gets sealed by
its own project's next security pass.

## The floor we stand on (implemented today)

- **Pausing is AG-UI-native, not a souk invention.** A provider ends its
  stream with `RUN_FINISHED` carrying the spec's own interrupt outcome;
  souk records `input-required` with the interrupts preserved
  (`souk/pause.py`, `handlers._handle_finish`).
- **A paused run blocks its thread** until resolved
  (`repo.get_active_run_for_thread`).
- **Resume is AG-UI-only.** `pause.is_resuming` is the single gate; the
  A2A surface always feeds it `resume=None`, so a delegating *agent* is
  structurally incapable of resolving an interrupt it was never meant to
  approve. This was a doctrine ("pausing means waiting on a human")
  enforced by shape; the model below gives it its missing semantics.
- **Lineage is recorded.** Delegated calls hang off the spawning thread
  and `Souk.get_thread_tree` walks it — discovery of a stuck leaf exists.
- **Actor chains are hop-signed and verifiable** (`identity.py`; each hop
  binds to the previous hop's hash).
- **The de facto resume credential is knowledge of `thread_id`.** Any
  AG-UI call naming the thread and carrying non-empty resume entries gets
  through. This is the part the rest of this document exists to replace.
- **Silent waiting has a clock.** `repo.fail_stalled_runs` treats a
  quiet-but-alive run as abandoned, which today puts an accidental upper
  bound on "my own human is thinking" — noted here as a known gap, not
  designed away yet.

## Rule zero: identifiers are never credentials

Identifiers are immutable — `thread_id` is woven into history, lineage
links and A2A task references, and can never change. Credentials must be
rotatable, revocable, replaceable. Therefore nothing whose only quality
is *being known* may authorize anything: a leaked `thread_id` would be a
permanent skeleton key with no remediation path, not even a bad one.

`thread_id` becomes a pure name. It may appear in logs, trees, task
references, anywhere — because knowing it grants nothing.

## The model

Five sentences, then the mechanics.

1. A thread's two parties are **a chain and a stall**.
2. The chain is the **incident-escalation path**: every delegation edge is
   declared, by the delegator, as *extending* the chain or *breaking* it —
   and an undeclared edge breaks it.
3. Intervention rights, cost, and visibility are three faces of one thing
   — **responsibility** — and travel together with the declaration.
4. The head of each unbroken chain segment is that segment's responsible
   party; an interrupt is resolvable by **the segment head's key or the
   stall's key**, whichever signs first.
5. **Unbound is public.** A thread opened with no caller key is, to core,
   a conversation held in the open square.

### Binding: three tiers, one mechanism

The only mechanism core adds is signature verification against a key
registered at thread creation. What differs is what the key is bound to:

- **An identity** — a caller with a long-lived keypair presents it; the
  chain head carries it down every extend-edge.
- **A thread and nothing else** — the client SDK generates a throwaway
  keypair per thread and registers the public half automatically. This is
  the anonymous tier done right: anonymity means the key is unlinked, not
  that there is no key. It rotates (old key signs new), it revokes, and it
  cannot be correlated across threads.
- **Nothing** — a bare standard client registers no key. Core treats the
  thread as public; whether a deployment fronts it with session auth is a
  serving-layer policy, which is where AG-UI itself locates
  authentication. No standard client is forced to deviate; presenting a
  key is opt-in, per `souk-no-forced-protocol-deviation`.

### Delegation: the edge declaration

Before a provider wires its agent to another agent, it owes itself one
question: *if the sub agent gets stuck or fails, can I carry on without
it?*

- **Yes → break the chain (the default).** The subtree below the break is
  the provider's own implementation, behind its A2A opacity: the provider
  is the new segment head. Its human resolves the subtree's interrupts;
  its KYOK offering funds the subtree's inference; the subtree is
  invisible in the original caller's thread tree; and if the subtree
  fails, the provider's own run delivers or fails *in the provider's
  name*. Suppliers are trade secrets and their failures are your
  failures — both follow from the same declaration.
- **No → extend the chain.** The escalation path stays connected: the
  segment head above retains sight of this branch and the right to walk
  down and resolve its interrupts, and the head's KYOK context continues
  to fund it — the work being done genuinely is the head's work.

The declaration is per-edge and recursive: a broken subtree's head faces
the same question for its own delegations. The resulting break-point
topology is a self-declared map of every provider's competence boundary —
and it is honest, because miscalibrated confidence is billed: declare
absorb and fail, and the failure lands on your run, your stall's record,
your invoice.

Bundling is what makes the bit incorruptible. Authority alone, an agent
would claim opacity while spending the caller's key; cost alone, it would
pass the bill while hiding the work. "The user pays but may not look" and
"the provider looks but does not pay" are both structurally unexpressible.

This amends `keep-your-own-key.md`'s "one-time context authorizes one run
**tree**": it authorizes one chain **segment**. souk still copies the
binding hop to hop, but stops at a break-edge — below it, the delegating
provider funds its own subtree or the sub call does not run.

### Verbs by position

| Party | Proof | May |
|---|---|---|
| Segment head | signature against the head key | resolve interrupts anywhere in its segment; read the segment's subtree |
| Intermediate/tail delegator | its place in the hop-signed chain | continue the conversation it already occupies (A2A call-again — exactly what it can do today, no more) |
| Stall keeper | the provider's registered key | resolve its own agent's interrupts |
| Anyone else | — | nothing on a bound thread; everything on an unbound one |

A resolution is itself a signed, persisted event: *which* key resolved an
interrupt becomes a verifiable fact of the thread's history. That closes
the answered-on-whose-authority gap, and it is the natural attachment
point for reputation ("trust evidence" in A2A-community terms): absorbed
failures and resolutions accrue to the key that signed them.

### Authorization is not disclosure

souk verifies the head's signature; the provider learns only that the
segment head resolved. Whether the provider learns *who* the head is
remains a separate, caller-controlled switch — the same two-layer split
KYOK's context relay already implements (relayed in memory, never
persisted, never volunteered). Implementers: do not print the head key to
the provider as a convenience. These are two switches by design.

### The coherence check: the dress-size relay

A provider breaks the chain to hide its suppliers, but its sub agent
pauses on a question only the end user can answer ("what size does the
customer wear?"). No new mechanism is needed:

1. Sub pauses. Its segment head is the provider (the break made it so).
2. The provider pauses **its own run toward its own caller** with its own
   interrupt, in its own words.
3. The user answers the provider; the provider — as its segment's head —
   resumes the sub with the answer.

The responsible party sources answers however it likes, including by
asking its own customer through its own storefront. The supplier stays
hidden, the escalation semantics hold, every hop stays signed. That the
model produces this pattern unprompted is the strongest evidence it is
cut correctly.

## Identifier-as-authorization audit

Rule zero's sweep of today's code — places where knowing an id is the
whole check (all verified 2026-08-18):

- **Resume by `thread_id`** — `protocols/agui.py` via `pause.is_resuming`.
  The subject of this document; replaced by the model above.
- **Reads by bare id** — `Souk.get_thread` / `get_thread_messages` /
  `get_thread_snapshot` / `get_thread_tree` (`core.py:912–926`),
  `get_run` / `get_run_events` (`core.py:952–956`), and the A2A surface's
  `GetTask`/resubscribe paths (`protocols/a2a.py:302–377`).

The write side adopts rule zero outright. The read side is a **choice
still open**: gating reads by the same tiers (bound thread → bound reads)
is the consistent extension, but reads have different failure economics
and the serving layer may be the right place for some of it. Until
decided, the honest statement is: *on an unbound thread, anyone who knows
the name can read the conversation — which is exactly what "unbound is
public" says out loud.*

## Deliberately left open

- **Key rotation mechanics** for identity-tier keys (already a parked
  topic; the per-thread tier sidesteps it by being disposable).
- **How the extend declaration rides an A2A delegation** without forcing
  a deviation — souk already intermediates delegation (it copies KYOK
  bindings hop to hop), so the declaration is souk-side state set by the
  delegating provider, not a new wire field; the exact surface is an
  implementation decision.
- **Expected-latency semantics for silent waiting** — the
  `fail_stalled_runs` clock currently bounds provider-side deliberation
  by accident. A provider saying "alive, waiting on my human, expect
  hours" deserves a first-class expression; not designed here.
- **Serving-layer session policy** for the unbound tier — the gateway's
  business, per the repo boundary.
