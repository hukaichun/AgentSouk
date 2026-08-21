# Responsibility chains

Part of [funduq's mechanisms](../mechanisms.md).

**Status: design, not implementation.** The design below is settled; no
code enforces it yet, and where it changes something the code currently
does (the `subject` field, notably), the code is the past and this page
is the direction. The earlier full record is
[`design/responsibility-chains.md`](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/responsibility-chains.md),
pinned to history; this page supersedes it where they differ.

## The problem

A user talks to a main agent; the main agent delegates to a sub-agent;
the sub-agent pauses for a human answer (`input-required`). Who may
answer, what proves they were entitled to, and what records that it was
them? The spec of neither protocol says; funduq has to.

## The frame: two entrances, one of them gated

Every seam has exactly two entrances — an **utterance**, or a deferred
call's **result** (see
[the design record](../design-records.md#every-seam-has-exactly-two-entrances-an-utterance-or-a-result)).
Responsibility chains gate the **result lane**: who may supply the
answer a paused ask is waiting for. Speaking is not gated among a
conversation's members — a member interjects or takes the next turn
without asking anyone's permission. What a chain adds on the utterance
side is only *membership itself*: on a bound thread (below), a
non-member cannot speak at all.

## A user is a position, not a type

funduq has one identity primitive — an Ed25519 keypair — and it already
plays two roles (agent provider, LLM provider). A "user" is the same
primitive in a third position: **the head of a responsibility segment**.
There is no user database, no account, no registration: a key exists by
being seen, and funduq recognizing a returning key never by itself
discloses anything to anyone.

The position is symmetric. When a main agent delegates over A2A and
signs the first hop of a *new* chain with its own key, it stands at the
head of the sub-segment exactly as a human stands at the head of theirs:
same signature, same answering rights over the segment's asks, same
view over the segment's threads. Humans usually sit at the root; that is
a social fact, not a mechanical one — the machinery never needs to know
whether a head is a person.

**Custody is deployment, not ontology.** Where the head's key lives is
the serving layer's business, and three models coexist:

- **self-held** — a file next to a CLI; works today;
- **passkey** — WebAuthn as the custodian; the platform authenticator
  signs once per session (its own envelope, verified at the bridge);
- **enterprise-custodied** — the gateway holds a per-user key behind
  SSO and signs after the IdP says yes.

All three converge through one bridge: the **session delegation
certificate**. The durable key `D` signs, once, a statement naming an
ephemeral in-page key `SK` and an expiry ("SK acts for me until T").
Thereafter SK signs everything — chain hops, resolutions — with the
certificate attached, and verification resolves SK's signatures to D's
authority. Rights attach to D and survive the session; SK is a glove.
This certificate is a new signed-payload family (chain extension alone
cannot express it: extending a chain is *provenance*, and anyone may do
it — delegating authority to a named key requires the durable key to
say so explicitly).

## The chain is only keys

A hop carries `{actorPublicKey, prevHash, iat, exp}` and a signature —
**no `subject`**. The shipped format's `subject` field was a self-claim:
the signer asserting whom the key represents, which funduq could record
but never verify — an assertion wearing the record's clothes. It is
removed from the design.

What replaces it splits into the two layers that were always distinct:

- **Authorization** is the chain itself: pure keys. Everything a chain
  is consulted for — answering rights, thread membership, segment
  visibility — reads keys and nothing else.
- **Disclosure** is a separate, opt-in credential: a **voucher**, signed
  by the party who actually knows ("key SK↔ employee_x, per this IdP" —
  signed by the gateway). Presented when the head *wants* to be known;
  absent otherwise. Pseudonymity is the default by construction, and
  "authorization is not disclosure" stops being a policy statement and
  becomes the file format.

The honest cost: until vouchers exist, every surface that shows a
caller shows a key fingerprint. An enterprise deployment will want
vouchers early; they are on the implementation list, not optional.

**And funduq stops summarizing.** `forwardedProps.caller` — the
verification digest funduq used to prepare for the agent (subject,
resolved actors, chain) — goes with `subject`. Its only real payload
was the raw chain, which is the *caller's* utterance, not funduq's: it
now rides to the agent verbatim, on the same two-slot pattern as any
caller-declared data (AG-UI callers write their own `forwardedProps`;
A2A callers use message metadata and funduq copies it over unchanged).
The agent verifies for itself — it never should have trusted a relay's
digest, and the digest's own docstring said so. funduq's inbound
verification serves funduq alone: refuse a chain that does not verify,
and copy the head key onto what needs it. With no digest there is also
no digest to forge, so the `verifiedActorChain` reserved-key defense
retires with it. What funduq does about caller identity is four verbs —
verify, copy the head, relay, refuse — and there is no fifth.

## Break and extend are behaviors, not metadata

An earlier shape of this design declared `break`/`extend` per delegation
edge. The declaration turned out to be redundant — **the chain's growth
is the declaration**:

- **extend** = the delegating party signs the incoming chain onward
  (`extend_chain`). The escalation path stays connected: the segment
  head's answering rights and visibility reach into the sub-thread.
- **break** = it does not. No flag, no field: silence severs. The
  delegator may start a fresh chain headed by itself (becoming the
  sub-segment's head), or delegate bare. The default is break because
  extension requires a positive act of signing.

Breaking also protects *downward*: carrying the user's chain through a
break edge would advertise whose work this ultimately is to
subcontractors the user never chose. Opacity cuts both ways — the
upstream cannot see the subtree, and the subtree cannot see upstream's
head.

Segment boundaries are therefore **derived, never registered**: a
sub-thread carrying the same head is the same segment; a new head or no
chain ends it. The forwarded-without-signing case keeps its existing
meaning (a silent hop: the chain verifies, the forwarder erased itself
— priced, not compelled).

## Lineage and responsibility are different layers

`referenceTaskIds` and `taskId` build **lineage** — funduq's own
structural record of which thread spawned which and which utterance
continues what. Lineage is always recorded, chain or no chain; a break
edge does not thin funduq's books. The chain decides **who may see and
act across** that structure. Passing a reference id down a break edge is
the delegator's own act of disclosure — it hands the sub-party an
upstream id it legitimately holds; not passing it means that edge never
appears in anyone's view of the tree.

## What binds, what locks, what stays open

**A thread binds at birth, immutably.** If its first run carries a
chain, the thread records {segment head `H`, serving provider `P`}.
Later runs cannot add or change the binding — an anonymously-opened
thread is never retroactively locked against its own opener.

- **Writing is membership.** On a bound thread only `H` and `P` may
  utter — members interject freely, non-members cannot speak. On an
  unbound thread, today's behavior stands unchanged.
- **Answering is authority.** A paused ask records, at pause time, its
  authority set from its run's chain: {segment head, the provider's own
  key}. A resolution is signed over `funduq-resolve:{ask_id}:{sha256 of
  the answer}` — the ask's funduq-minted, single-use id is the nonce, so
  no challenge round-trip exists to lose; replay hits an ask already
  consumed. Who resolved, under whose authority, is recorded.
- **Reading a known id stays open at the core.** Locking reads at the
  door would gate confidentiality funduq cannot actually deliver (the
  database is readable by its operator; real secrecy is encryption,
  which this is not) while breaking every unauthenticated read a
  standard client makes. Confidentiality is the host's layer: a gateway
  that authenticates sessions gates reads as deployment policy. The
  core's split is: **it enforces integrity (writes, resolutions) with
  signatures; it leaves confidentiality (reads) to the deployment.**
- **Discovery respects segments.** Tree and listing queries stop at
  segment boundaries: a broken-off subtree does not appear in an
  upstream head's view. Invisibility is honest about what it is — not
  being enumerated — never a pretense of secrecy for a party who
  already holds the id.

## The complete invention list

Three signed-payload families and two enforcement points; nothing else.

1. **Hop** — `{actorPublicKey, prevHash, iat, exp}`, signed. Exists
   (minus `subject`). The chain's growth is the responsibility
   declaration.
2. **Session delegation certificate** — durable key names an ephemeral
   key with an expiry. New. Custody's bridge.
3. **Voucher** — a knowing party binds a key to a name. New, opt-in.
   Disclosure's channel.

Enforcement: copy the segment head onto asks and threads at their
birth; filter discovery by segment. No funduq-authored summary exists
anywhere in the design: the chain reaches the agent as the caller's own
words, and everything else — who is a user, where break points are, who
may answer — is derived from where signatures actually reached.

## Deliberately out of scope here

Funding (which KYOK offering pays for a subtree) is a separate topic
with its own discussion; nothing on this page decides it. Cancel is
outside the entrance taxonomy entirely — a control request about a run,
not an input, and no precedent for gating one.

## Design records

- [Every seam has exactly two entrances](../design-records.md#every-seam-has-exactly-two-entrances-an-utterance-or-a-result)
- [Rule zero: identifiers are never credentials](../design-records.md#rule-zero-identifiers-are-never-credentials)
- [Anonymity means the key is unlinked, not that there is no key](../design-records.md#anonymity-means-the-key-is-unlinked-not-that-there-is-no-key)
- [One question per delegation edge decides the whole tree](../design-records.md#one-question-per-delegation-edge-decides-the-whole-tree)
- [Authorization is not disclosure](../design-records.md#authorization-is-not-disclosure)
