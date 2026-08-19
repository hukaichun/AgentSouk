# The SDKs

Callers need no SDK — that is [the contract](integration-contract.md)'s promise, not an
omission. The SDKs in this repository serve the two parties that connect
*out* to souk, and both are **pure contract packages**: what each side
agrees on, as importable code and models, with no transport inside.
Wrapping either in a network is a downstream job.

## souk-provider-sdk

The agent provider's side of the agreement: the Ed25519 identity and
everything it signs, the `SoukLink` port a transport implements, the
provider's own worker loop, the delivered envelopes and forwarded-props
models, and the chain verifier — each an independent twin of souk's
implementation, pinned by the published vectors.
→ [Details](sdks/provider-sdk.md)

## souk-llm-provider-sdk

The LLM provider's side, inheriting the same discipline: the
`SoukLLMLink` port, the delivered-completion envelope, the structured
refusal, and its own registration payloads. Identity is identity — the
keypair class comes from the provider SDK, because one keypair may be
both kinds of provider at once.
→ [Details](sdks/llm-provider-sdk.md)

## The wire, simulated without a transport

Because the frame a transport carries is exactly
`model_dump(by_alias=True)` of the delivered envelopes — and every signed
payload is a pure function to bytes — the wire itself can be exercised
with no socket anywhere: serialize each crossing to JSON bytes, rebuild
it on the far side from the published shape, and run the whole loop
in-process. `souk/tests/test_wire_loopback.py` does exactly that, as a
kept proof rather than a claim: a run travels from a standard AG-UI call
through the connect handshake, the byte-framed delivery, and byte-framed
events back; a KYOK completion crosses the same way. If a frame shape
drifts, that file fails the way a deployed gateway would — before any
gateway exists.
