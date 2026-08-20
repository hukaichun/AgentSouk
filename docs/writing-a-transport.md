# Writing a transport

souk's core is network-free: it hands back objects and pure functions,
and putting them on a wire is a downstream job. This page is what that
job actually involves — the handshake in order, with the exact calls,
and the parts souk deliberately leaves to you.

Everything a transport must produce or validate is pinned in
[`contract-vectors.json`](contract-vectors.json), so you can implement
this in any language without reading souk's source.

## The opening handshake, in order

Four steps, and the order is the security property. Getting it right
matters more here than anywhere else in the contract, because this is
the one exchange that decides whether either side is talking to who it
thinks.

**1. souk mints a challenge.**

```python
challenge = souk.issue_connect_challenge()
```

A 128-bit hex nonce, single-use, valid for 60 seconds. It is souk's
contribution to freshness, and it exists because a signature whose only
liveness is a self-chosen timestamp is replayable by anyone on the path
— see [the design record](design-records.md#the-verifier-chooses-the-freshness).

**2. The provider signs both nonces and its names.**

```python
proof = identity.sign_connect(souk_nonce, provider_nonce, names)
```

The provider contributes a nonce of its own. The signed bytes are
`souk-connect-provider:{souk_nonce}:{provider_nonce}:{sorted names}` —
the names are in the proof, so a captured signature cannot be replayed
to serve a different agent.

**3. souk answers, and the provider verifies before sending anything.**

This is the step transports get wrong, so it is worth stating exactly.
souk's answer is a signature over
`souk-connect-souk:{souk_nonce}:{provider_nonce}` — a **distinct role
tag**, so neither side's proof can be reflected back as the other's.

There is no souk method that returns it. Compose it:

```python
from souk.identity import souk_connect_signing_payload
answer = souk.sign(souk_connect_signing_payload(challenge, provider_nonce))
```

and the provider verifies it against its pinned souk key:

```python
from souk_provider_sdk import verify_signature, souk_connect_payload
assert verify_signature(souk_public_key, answer, souk_connect_payload(challenge, provider_nonce))
```

!!! warning "souk does not enforce this half"
    Nothing in souk requires a provider to check the answer, and no
    shipped transport in this repository does. Skipping it still gets
    you a working connection — to *any* souk, including one that is not
    the one you meant. This is the half of the handshake that protects
    the provider, and it is yours to implement.

    `Souk.sign` raises when souk has no identity key configured, so a
    souk that cannot be pinned fails loudly rather than silently
    answering nothing.

**4. Attach.**

```python
await souk.attach_provider(
    provider, agent_names,
    challenge=challenge, provider_nonce=provider_nonce, proof=proof,
)
```

The proof is verified **before** the registered-names check, so an
attach that cannot prove itself never learns whether a name is
registered. There is no way to switch this off: a connection that
exposes no `sign_connect` and supplies no proof is refused. In-process
connections take the same path — sharing a process is not a reason to
skip identity.

## Reconnecting without killing your replacement

```python
await souk.detach_provider(public_key, connection=old_link)
```

Pass the old connection. Without it, **every** name that key serves goes
offline — including a replacement connection that has already
re-attached, which is exactly the case a reconnect produces. Cleanup
that does not name what it is cleaning up takes down the thing that
replaced it.

Note the asymmetry: `detach_provider` is a coroutine and must be
awaited, while `detach_llm_provider` is synchronous.

## Carrying a run down, and the ack back

An offer is one call carrying the run envelope, and the answer is
**three-valued**: accepted, declined-because-full, or permanently
refused with a reason. A transport that collapses this into one bit
re-creates a bug souk already had — runs re-offered forever, reading as
`queued` from every vantage point while only the provider's log knew the
truth. Whatever framing you choose, all three values must survive it.

Everything else about how the answer travels is yours: framing,
correlation, backpressure, reconnect policy. souk asks a question and
reads an answer; it has no opinion on the envelope.

## Relaying events

Dump typed events with `exclude_none=True`. A default dump injects
`timestamp: null` and `rawEvent: null` into the caller's stream; with
that flag the round trip is byte-identical to the input. Read only the
fields you are deciding on.

Be aware of the open edge here: an event that does not validate as
AG-UI ends the run, so a provider running a newer AG-UI than souk can be
cut off by an event type souk has not heard of. See
[the design record](design-records.md#wrapping-an-unknown-event-in-rawevent-is-quiet-corruption)
before working around it.

## Prove it against the vectors

[`contract-vectors.json`](contract-vectors.json) publishes the seven
signed payload families, the two wire envelopes (`delivered-run`,
`delivered-completion`) and the actor-chain form, each with deterministic
signatures under a published test key. souk's own suites replay them,
and so do both SDKs' — as independent twins that do not import souk. If
your transport reproduces the vectors byte-for-byte, it is correct by
the same standard souk holds itself to.
