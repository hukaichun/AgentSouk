# Contract and identity

Part of [core components](../core-components.md).

Signing lives with whoever holds a key; **core's half is verification**.
This page describes how each verifying piece works.

## Signed payloads

Every signed operation reduces to the same primitive: build a canonical
byte string, verify an Ed25519 signature over exactly those bytes. The
payloads are colon-joined text under a **domain tag** —
`souk-register:{sorted names}:{timestamp}`,
`souk-delete-agent:{name}:{timestamp}`,
`souk-kyok-call:{token}:{timestamp}:{sha256 of body}`, and so on for the
seven families — names sorted so order can't change the bytes, the tag
first so a signature captured for one purpose is meaningless for
another. Timestamped families are accepted within a ±60s freshness
window of souk's clock.

## Link-open challenges

Connect authentication cannot ride a timestamp (self-chosen freshness is
replayable for its whole window), so it answers a challenge souk chose:
souk mints a random single-use nonce and remembers it with its issue
time; the connecting provider signs
`souk-connect-provider:{souk nonce}:{provider nonce}:{sorted names}`;
verification **consumes** the nonce (a second use fails) and checks it
was issued recently. souk answers with its own signature — its
`SoukIdentity` keypair over `souk-connect-souk:{both nonces}` — which the
provider checks against the souk key it pinned. In-process connections
go through the identical ceremony automatically: any connection that can
sign is challenged at attach.

## Actor chain verification

A chain is a list of JWTs. The verifier walks it in order: for each hop,
read the signer's public key out of the (not yet trusted) payload,
verify the JWT's signature under exactly that key, then check the hop's
`prevHash` equals the sha256 of the previous hop's full text, and that
the `subject` never changes. Expiry is enforced on the **last** hop
only — earlier hops are provenance, not standing authorization. The
result is the subject plus each signer's key in order; souk additionally
resolves each key against the roster to name registered agents, which
the SDK's twin verifier deliberately does not (it has no roster).

## Envelope models

Every structure souk puts on a wire is a pydantic model with camelCase
aliases: `model_dump(by_alias=True)` **is** the frame a transport
carries, `model_validate` rebuilds it. `models.py` declares the refs and
the claimed-run shape; `props.py` declares souk's two forwarded-props
additions and builds them in one place for both caller doors. Translation
from souk's internal objects to the delivered forms is stated once, as
classmethods on the delivered models, read by attribute so no import
crosses the boundary.

## The twins and the guards

Everything above is deliberately implemented twice: the provider SDKs
carry independent twins — payload builders, a chain verifier, the
delivered envelopes and props models — and neither side imports the
other, so each suite is a second opinion on the bytes rather than an
echo. Agreement is pinned by
[`contract-vectors.json`](../contract-vectors.json): inputs → exact
payload bytes → deterministic signatures under a published test key,
plus a fixed-time chain and the canonical wire frames, consumed by all
three test suites and replayable in any language. Two CI guards keep the
set complete — every domain tag must have a vector family, and the SDKs
must validate every structure souk puts on the wire — because both gaps
were shipped and paid for before the guards existed.
