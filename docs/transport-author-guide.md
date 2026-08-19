# Writing a transport

What it takes to carry souk's provider contract over a wire of your own —
a WebSocket, gRPC, a message queue, anything. Everything your transport
must agree on is published as data; everything about *how* you carry it is
yours. This guide is the narrative over those pieces; the byte-level
authority is `docs/contract-vectors.json`, which your implementation should
replay in its own test suite (that file has caught every drift found so
far — before integrators' production did).

## The shape of the job

A transport connects two halves that never import each other:

- **souk's side**: something satisfying `ConnectedProvider` (or
  `ConnectedLLMProvider` for the KYOK side) that souk attaches — typically
  your gateway's socket wrapper.
- **the provider's side**: a subclass of `souk_provider_sdk.SoukLink`
  (or `souk_llm_provider_sdk.SoukLLMLink`) driving the provider's own code.

In-process is a transport too — `InProcessLink` / `InProcessLLMProvider`
are the one-process case of the same ports, and they get no shortcut a
remote link doesn't get. If you find yourself special-casing one side,
the boundary is drawn wrong.

## Opening: authenticate against a challenge the verifier chose

Do not invent a connect payload — the invented ones have all been
replayable (issues #44, #75). The published family:

1. Ask souk for a challenge (`souk.issue_connect_challenge()` on the
   gateway side) and relay the nonce to the provider process.
2. The provider answers with `ProviderIdentity.sign_connect(souk_nonce,
   provider_nonce, names)` — its own fresh nonce alongside, and the names
   it intends to serve bound in.
3. Relay souk's answering proof back — souk signs
   `souk_connect_payload(souk_nonce, provider_nonce)` with its identity
   key — and verify it with `verify_signature` against the souk public key
   the provider pinned, *before* sending anything worth stealing.
4. Call `attach_provider(link, names, challenge=…, provider_nonce=…,
   proof=…)`. The challenge is single-use and expires with the freshness
   window.

Frame ordering, retries, reconnect semantics: yours. On reconnect,
remember souk holds **one connection per role** — a re-attach under the
same key replaces the old connection, and your cleanup for a replaced
connection should pass it to `detach_provider(key, connection=old)` so it
never takes the replacement down with it.

## Carrying a run down

The frame for an offered run is exactly
`DeliveredRun.model_dump(by_alias=True)` — camelCase keys, `runInput` in
AG-UI's own camelCase form — and the far side rebuilds it with
`DeliveredRun.model_validate(frame)`. The canonical frame lives in the
vectors' `wire` section; round-trip it in your tests and a renamed field
fails you before it fails a user.

Inside `runInput.forwardedProps`, two keys are souk's own inventions and
have SDK models to validate with — `CallerProps` (note `chain` may be
null) and `KyokForwardedProps`, both in `souk_provider_sdk.props`. Do not
restate these shapes; a restated copy once drifted on one field's
nullability and silently dropped verified caller identities.

The provider answers an offer with three values, and your ack frame must
carry all three: `True` (accepted), `False` (full right now — souk keeps
the run and offers again later), or `Refusal(reason)` (permanent — souk
fails the run with the provider's reason verbatim and stops re-offering).
Collapsing the last two into one bit re-creates the run-stuck-queued bug
the three-state answer exists to fix.

## Carrying results back

`report_event(run_id, event)` relays AG-UI events; `finish_run(run_id)`
ends the stream; `cancel(run_id)` arrives from souk as a *request* — the
provider decides the outcome, souk records what it then observes.
`thread_messages(thread_id)` is the pull-side query a provider may make.

## The KYOK side

A completion travels as
`DeliveredCompletion.model_dump(by_alias=True)` (also vector-pinned) down
to the LLM provider's handler, and OpenAI chat-completion chunks stream
back. Two envelopes matter:

- The handler refusing structurally raises `CompletionRefused(payload)`;
  your transport must carry the payload **intact** — souk relays it to
  the calling agent as data (in-stream `{"error": …}` or on
  `KyokRejected.refusal`) and never interprets it. The vocabulary inside
  is the parties' own; the library defines only the envelope.
- The agent provider calling for a completion signs `souk-kyok-call`
  over the bearer token, a timestamp, and the body hash — the agent's
  key, not the LLM provider's.

## Prove it against the vectors

`docs/contract-vectors.json` carries, for every contract surface, inputs →
exact bytes → a deterministic signature under a published test key:

- the seven signed payload families (`vectors`),
- a fixed-time actor chain both verifiers must accept (`chains`),
- the canonical wire frames (`wire`).

A third-party implementation in any language replays the file: build each
payload from `inputs`, assert the exact bytes, reproduce or verify the
signature, round-trip the frames. souk's own suites consume the same file,
so it cannot drift from the implementation — pin your transport to it and
the two of you cannot drift from each other.

## What stays yours

Frame type tags, correlation ids, multiplexing, backpressure, TLS,
reconnect policy, and every timing decision. souk publishes what the bytes
mean, never how they are carried — if this guide seems to be missing a
frame table, that is the boundary working as intended.
