# Actor chain

Part of [souk's mechanisms](../mechanisms.md).

A chain answers "on whose behalf, through whose hands". Each hop is an
EdDSA JWT carrying the `subject` the chain vouches for, the signer's own
`actorPublicKey`, a `prevHash` — the sha256 of the previous hop's full
JWT, null on the first — and `iat`/`exp`. Each hop is signed by the key
it names; extending a chain appends a hop that carries the subject
forward unchanged and hash-links to the tail. The existing hops are never
modified.

## Verification

Both sides verify independently — `souk.identity.verify_actor_chain` in
core, `souk_provider_sdk.verify_chain` in the SDK — under the same rules:
every hop's signature under its own embedded key, the hash linkage, one
subject throughout, and expiry enforced **on the last hop only**. Earlier
hops are provenance, not standing authorization, so a run paused on a
human longer than a hop's TTL stays resumable. Rejected: a forged hop, a
spliced or reordered chain, a subject swapped partway. A fixed-time chain
in [`contract-vectors.json`](../contract-vectors.json) pins both
verifiers byte-for-byte.

## What a chain proves — and deliberately does not

Verification proves nobody rewrote the hops that exist. Two things it
does not prove, by design:

- **The first hop is a claim.** The subject is asserted by whoever signed
  hop zero; souk cannot know how that party came to trust it. The
  weakness closes where the verifier *is* the subject — in KYOK's
  personal-key deployment, the party verifying the chain knows which
  first-hop key is genuinely its own agency.
- **A silent hop is an omission, not a break.** A party that forwards a
  chain without extending it produces a chain that still verifies — it
  has merely erased itself from the path. souk does not force anyone to
  sign; enforcement belongs to the chain's consumer, whose policy knows
  the expected call graph and refuses one with a missing link. In KYOK
  that consumer controls the completions, so signing is not compelled —
  it is priced.

souk verifies chains and relays them; it never signs on anyone's behalf
and never vouches for a subject. (souk countersigning the delegations it
routes is a recorded direction — useful for a signed audit trail and for
chains crossing federated souks — not a built mechanism.)
