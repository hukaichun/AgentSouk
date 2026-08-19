# souk-llm-provider-sdk

Part of [the SDKs](../sdks.md).

What an LLM provider and souk agree on — the party that holds a real key
and answers [KYOK](../mechanisms/kyok.md) completions. Two dependencies:
`souk-provider-sdk` (identity is identity; the keypair class is shared
because one keypair may serve agents and models at once) and `openai`
(the wire shapes are OpenAI's chat completions — types only, no client
is constructed here). No transport, no souk, same fitness test.

## Registration and identity

`sign_llm_registration` / `llm_registration_payload` and their deletion
counterparts — the offering roster's payloads, distinct domain tags from
the agent family, twins of souk's builders, vector-pinned. Connect
authentication reuses the shared `sign_connect`.

## The port

`SoukLLMLink` is the abstract port: the base translates souk's
completion request into a `DeliveredCompletion` (run id, the proven
calling-agent identity, which model was addressed, the opaque `context`,
the actor chain), and `serve` is where the provider's own code takes
over — the interposition point every completion passes through before
any money moves. `InProcessLLMProvider` is the in-process transport,
driving a plain `CompletionHandler` (an async function from
`DeliveredCompletion` to a stream of chunks).

## Refusing structurally

`CompletionRefused(payload)` raised from a handler travels to the
calling agent as data — souk relays the payload intact and never reads
it. The library defines only the envelope; what a refusal *means*
(a ceiling reached, a chain not served, anything) is vocabulary between
the provider and its callers. Policy itself — spend ceilings, model
allow-lists, chain checks via `verify_chain` — is deliberately the
provider's own few lines, not a library feature; the package README
carries a worked, test-pinned example.
