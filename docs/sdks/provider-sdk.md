# souk-provider-sdk

Part of [the SDKs](../sdks.md).

What an agent provider and souk agree on, stated from the provider's
side. Dependencies are `cryptography`, `pyjwt`, `ag-ui-protocol` — no
transport library, and no souk: not depending on souk is what makes this
package a second opinion on the bytes rather than an echo, and a fitness
test keeps it that way.

## Identity and signing

`ProviderIdentity` wraps the Ed25519 keypair (generate, or load-or-create
from a key file so a restarted process keeps its identity; `public_key`
is the 64-char hex). Around it, builders for every payload the provider
signs: registration and deletion (timestamped), the link-open connect
proof (challenge-answered — `sign_connect` over souk's nonce, the
provider's own nonce, and the names to serve), and the per-call KYOK
signature. Each builder is the independent twin of souk's, and
reproduces the published vectors byte-for-byte, deterministic signatures
included.

## The port and the loop

`SoukLink` is the abstract port a transport implements: `offer` a
delivered run (answering accepted / declined / `Refusal`), `cancel`,
`report_event`, `finish_run`, `thread_messages`. The base class states
the one translation every transport needs — souk's claimed-run object
becomes a `DeliveredRun`, and an input that doesn't validate is a
permanent `Refusal`, not a transient decline. `InProcessLink` is the
in-process transport (in-process is a transport, not a special case);
`ProviderRuntime` is the worker loop that queues delivered runs, executes
each agent's `run_stream`, and reports events back through whatever link
it is on.

## The envelopes

`DeliveredRun` is both what the runtime consumes and the declared wire
frame (`model_dump(by_alias=True)` / `model_validate`); `CallerProps`,
`VerifiedActor` and `KyokForwardedProps` declare souk's two
forwarded-props additions so no provider restates them — a restated copy
once drifted on one field's nullability and silently dropped verified
caller identities.

## Chain machinery

Both halves: `sign_hop` / `new_chain` / `extend_chain` to participate in
an actor chain, and `verify_chain` to police one — the twin of souk's
verifier, same rules, no roster resolution, so an LLM provider or any
consumer can verify a delegation path without importing souk.
