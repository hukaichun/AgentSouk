# Contract and identity

Part of [core components](../core-components.md).

Signing lives with whoever holds a key; **core's half is verification** —
which is why this component exists. `souk/identity.py` verifies the seven
signed payload families (each under its own domain tag, timestamped or
challenge-answered as the family requires), issues and consumes the
single-use link-open challenges, verifies actor chains, and holds
`SoukIdentity` — the one place souk itself signs: answering a connecting
provider's challenge (and, when the parked direction lands, its own hops
on a chain).

The envelope models are the other half of the contract surface:
`models.py` (`AgentRef`, `LlmRef`, `ClaimedRun`, the summaries) and
`props.py` (`CallerProps`, `VerifiedActor`, the forwarded-props builder)
declare every structure souk puts on the wire, with
`model_dump(by_alias=True)` as the frame a transport carries.

Everything here is deliberately duplicated once: the provider SDKs hold
independent twins — payload builders, a chain verifier, the delivered
envelopes and props models — and neither side imports the other. The
twins are pinned byte-for-byte by
[`contract-vectors.json`](../contract-vectors.json), which all three test
suites consume, and two CI guards keep the set complete: every domain tag
in `souk.identity` must have a vector family, and the SDKs must be able
to validate every structure souk puts on the wire. Both guards exist
because their absence was paid for twice.
