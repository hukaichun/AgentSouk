# Trust and identity: who proves what to whom

Everything souk verifies, in one place. The mechanisms below are spread
across `souk/identity.py`, the two provider SDKs, and
`docs/contract-vectors.json`; this document is the model they add up to —
including what each proof deliberately does *not* establish. Line-level
authority stays with the code and the vectors; when this document and they
disagree, one of them needs fixing, deliberately.

## Who has an identity

- **A provider is its Ed25519 keypair.** The public key is the address; a
  `name` is deliberately not an identity (it is not exclusive). Agent
  providers and LLM providers use the same machinery
  (`souk_provider_sdk.ProviderIdentity`) — one keypair may be both at once.
  A short `fingerprint` derived from the key supports human-friendly
  resolution; two keys colliding on one fingerprint is an error, not a
  merge (trust-on-first-use).
- **A souk has an identity too** (`SoukIdentity`, configured, never
  generated silently), so a provider can pin the souk it means to serve by
  its public key and detect an imposter before producing anything worth
  stealing.
- **A caller does not, yet.** The subject of an actor chain is asserted,
  not proven — see "the first hop is a claim" below. A caller-identity
  design is an acknowledged open front, not an oversight.

## What gets signed: the seven payload families

Every byte string souk verifies a signature over is published three ways:
as a builder in `souk.identity`, as an **independent twin** in the provider
SDK (neither package imports the other — the deliberate duplication is what
lets each suite catch the other drifting), and as a vector family in
`docs/contract-vectors.json` that any implementation, in any language, can
replay byte-for-byte (Ed25519 is deterministic, so even the signatures
reproduce exactly).

| family (domain tag) | who signs it | to authorize |
|---|---|---|
| `souk-register` | agent provider | registering agent names |
| `souk-register-llm` | LLM provider | registering model offerings |
| `souk-delete-agent` | agent provider | deleting one agent record |
| `souk-delete-llm` | LLM provider | deleting one offering record |
| `souk-kyok-call` | **agent** provider | one KYOK completion call |
| `souk-connect-provider` | connecting provider | opening a link (below) |
| `souk-connect-souk` | souk itself | answering a link-open (below) |

The domain tag is load-bearing: it is what keeps a captured registration
signature from being replayable as a deletion order, or either roster's
registration as the other's. A fitness test scans `souk.identity` for tags
and fails if any lacks a vector family — an unpublished payload family is a
CI failure now, not an integrator's production incident (it was the
latter twice before that guard existed).

## Opening a link: the verifier chooses the freshness

Registration and deletion sign over a timestamp, checked against a 60s
freshness window — good enough for operations that are idempotent or
singular. **Connect authentication is not in that family, on purpose.** A
signature whose only liveness is a self-chosen timestamp is replayable for
the whole window by anyone who observed it — and observers are not exotic:
enterprise proxies terminate TLS on the path (which is also why channel
binding was ruled out). This exact hole shipped twice: once in souk's own
early gateway (issue #44), once in an integrator's transport built from
the only worked example then visible (issue #75).

The published shape is challenge-response, both directions:

1. souk mints a **single-use nonce** (`issue_connect_challenge`), expiring
   with the freshness window. A transport relays it to the far side.
2. The provider signs `souk-connect-provider:{souk_nonce}:{provider_nonce}:{names}`
   — souk's nonce makes a recording worthless; the provider's own nonce is
   its challenge for souk's answer; the sorted names bind what it intends
   to serve so they cannot be altered in flight.
3. souk answers with its own signature over
   `souk-connect-souk:{souk_nonce}:{provider_nonce}` — verified by the
   provider against the souk key it pinned. The role tags differ so
   neither proof can be reflected as the other.
4. `attach` verifies the proof and consumes the challenge.

**In-process is not trusted either.** The in-process links expose
`sign_connect`, and `attach` challenges any connection that can sign —
automatically, so an embedder authenticates without writing a line. A
connection offering a proof always has it verified; one offering none is
refused. A `require_connect_proof` migration switch existed while the
handshake landed and was removed (#102): one handshake everywhere is what
lets a provider in any language implement it once against the published
vectors. Sequencing — who sends which frame when, reconnects,
multiplexing — is the transport's property; souk publishes the bytes,
never the choreography.

## Actor chains: provenance, hop by hop

A chain answers "on whose behalf, through whose hands". Each hop is an
EdDSA JWT whose payload carries the `subject` (who the chain vouches for),
the signer's own `actorPublicKey`, a `prevHash` (sha256 of the previous
hop's full JWT; null on the first), and `iat`/`exp`. Each hop is signed by
the key it names; extending a chain appends a hop that carries the subject
forward unchanged and hash-links to the tail. Verification —
`souk.identity.verify_actor_chain` in core, `souk_provider_sdk.verify_chain`
as its independent twin, both pinned by the `chains` vector family —
checks every hop's signature under its own embedded key, the hash linkage,
one subject throughout, and expiry **on the last hop only**: earlier hops
are provenance, not standing authorization, so a run paused on a human
longer than a hop's TTL stays resumable.

What verification proves: nobody rewrote, reordered, truncated-and-spliced,
or re-subjected the hops that exist. What it deliberately does not prove:

- **The first hop is a claim.** The chain's subject is asserted by whoever
  signed hop zero; souk cannot know how that party came to trust the user.
  The weakness closes where the verifier *is* the subject — in KYOK's
  personal-key deployment, the user's own LLM provider knows which first-hop
  key is genuinely their agency, and the only party able to verify a
  subject claim is the subject.
- **A silent hop is an omission, not a break.** A provider that forwards a
  chain without extending it produces a chain that still verifies — it has
  merely erased itself from the path. souk does not force anyone to sign
  ("souk never decides on a provider's behalf"); enforcement belongs to
  the chain's consumer, whose policy knows the expected call graph and
  refuses one with a missing link. In KYOK that consumer controls the
  money: an agent whose chain doesn't match gets no completions, so
  signing is not compelled, it is priced.
- **Recorded direction, not built:** souk itself could countersign each
  delegation it routes (it holds the only record of run parenthood, and
  the pin-souk machinery makes its hops verifiable). Under today's
  topology this adds little — everything reaches the KYOK verifier through
  souk anyway, so souk's signature mostly restates trust the verifier
  already extends — but it becomes load-bearing for a signed audit trail
  (issue #73) and for chains crossing federated souks. It waits for those.

## KYOK's trust seams

Keep-your-own-key's full story is `keep-your-own-key.md`; the identity
facts that belong in this model:

- The **KYOK token** is signed, not sealed: HMAC by souk, readable by the
  agent provider that carries it. Nothing goes in the body that the agent
  must not learn; the caller's `context` travels souk-internally and never
  enters the token or the durable record's read-back roads.
- The **call signature** (`souk-kyok-call`) is the *agent provider's* — it
  proves the agent named in the token made this call, binding the bearer
  token, a timestamp, and a hash of the request body. The LLM provider
  serves the completion; it never signs this payload.
- The **LLM provider is the policy seam**, and holds the tools without
  importing souk: `verify_chain` to police delegation paths, the opaque
  `context` to know whose budget a run spends, and `CompletionRefused` to
  answer with a structured refusal that souk relays without interpreting —
  souk publishes the envelope, never the vocabulary.

## What souk never asserts

souk verifies; it does not vouch. It never records an outcome it has not
observed, never signs on a provider's behalf, never interprets a context
or a refusal payload, and never treats sharing a process as a reason to
skip any of the above. Where souk *does* observe — completions relayed,
offers refused, providers misdeclaring — it counts
(`RunBroker.quality`, `KyokRelay.quality`) and judges nothing.
