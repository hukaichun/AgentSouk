# souk's mechanisms

Six mechanisms are souk's own inventions. Everything else souk does is
either a standard protocol carried unchanged (see
[the integration contract](integration-contract.md)) or an implementation detail of one
of these six.

## Identity is an Ed25519 keypair

A provider *is* its keypair — a name is deliberately not an identity —
and every operation that changes what souk will serve is signed:
registering, deleting, opening a link, calling for a completion. souk has
a keypair of its own, so a provider can pin the souk it means to serve.
→ [Details](mechanisms/identity.md)

## Actor chain

A run can carry provenance: a hash-linked chain of self-signed hops
recording on whose behalf, through whose hands. souk verifies chains and
carries them; it does not vouch for them — what a chain proves and
deliberately does not prove is part of the mechanism.
→ [Details](mechanisms/actor-chain.md)

## Runs and cancels are requests

Everything souk sends a provider is a request, never a command. An
offered run can be declined or refused; a cancel can be complied with or
not; souk records only the outcomes it observes and never decides on a
provider's behalf.
→ [Details](mechanisms/requests.md)

## Provider quality counters

Capacity is a provider's own declaration, and souk counts what it then
observes — offers unanswered, runs abandoned, completions refused or
failed — per provider, judging nothing. The counters are the record a
serving layer or a policy can act on.
→ [Details](mechanisms/quality.md)

## Keep your own key (KYOK)

A run can be bound to an LLM offering so that the agent working it calls
"an LLM" without ever holding the caller's credential: souk relays each
completion to the bound provider, which serves it with its own key and
its own policy.
→ [Details](mechanisms/kyok.md)

## Responsibility chains

*Design, not implementation.* When a run pauses for a human, who may
answer? Responsibility chains make that an explicit, per-edge decision —
each delegation edge can carry, break, or extend the right to act, with
its cost and visibility bundled.
→ [Details](mechanisms/responsibility-chains.md)
