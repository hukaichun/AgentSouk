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
  sign, and takes no position on whether it should have: whether the full
  chain must be carried at every hop is a convention the agent providers
  and LLM providers involved agree between themselves. souk carries and
  verifies whatever chain arrives, and leaves what to accept to the
  parties.

souk verifies chains and relays them; it never signs on anyone's behalf
and never vouches for a subject.

## souk signs as an identity too — **not implemented yet**

> **Status: decided direction, no code.** Tracked here so the gap is
> visible; the TTL semantics below block it and are deliberately parked.

souk is an identity like any other (`SoukIdentity`, the key providers
already pin), so it can bear the same responsibility on a chain that a
provider does: append one standard hop, signed with its own key, each
time it dispatches a run — no new claim, no role marker, no format
extension. What that buys:

- **"Routed through souk" becomes verifiable.** A consumer that pins a
  souk key can require its hops in the path; a chain that bypassed souk,
  or was fabricated whole, doesn't have them.
- **A silent hop becomes structurally visible.** With souk signing every
  dispatch, a fully-signed chain alternates souk and provider hops; a
  provider that forwarded without signing leaves two consecutive souk
  hops — witnessed, without souk naming anyone.
- **Federation needs no extra design.** A chain crossing several souks
  carries each souk's own hops; consumers pin the souks they trust, the
  same act as pinning one.

What blocks it: hop-expiry semantics. souk's hop would often be the last
hop at delivery time, and verification enforces expiry on the last hop —
a chain read again late in a long run would fail on souk's own stale
hop. (The edge exists today for provider hops too; souk signing every
dispatch would make it constant.) That discussion is parked; the
mechanism waits for it.
