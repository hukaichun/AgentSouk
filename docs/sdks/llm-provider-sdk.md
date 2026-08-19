# souk-llm-provider-sdk

Part of [the SDKs](../sdks.md).

What an LLM provider and souk agree on — the party that holds a real key
and answers [KYOK](../mechanisms/kyok.md) completions. Two dependencies:
`souk-provider-sdk` (identity is identity; the keypair class is shared
because one keypair may serve agents and models at once) and `openai`
(the wire shapes are OpenAI's chat completions — types only, no client
is constructed here). No transport, no souk, same fitness test.

## Identity and signing

`sign_llm_registration` / `llm_registration_payload` and their deletion
counterparts — the offering roster's payloads, distinct domain tags from
the agent family, twins of souk's builders, vector-pinned. Connect
authentication reuses the shared `sign_connect`.

## The port and the worker

`SoukLLMLink` is the abstract port a transport implements: the base
translates souk's completion request into a `DeliveredCompletion`, and
`serve` is where the provider's own code takes over — the interposition
point every completion passes through before any money moves. The worker
is a plain `CompletionHandler` — an async function from
`DeliveredCompletion` to a stream of chunks — and
`InProcessLLMProvider` is the in-process transport driving one
(in-process is a transport, not a special case, same as the agent side).

## The envelopes

`DeliveredCompletion` is both what the handler consumes and the declared
wire frame (`model_dump(by_alias=True)` / `model_validate`): the run id,
the proven calling-agent identity, which model was addressed, the opaque
`context` relayed untouched, and the run's actor chain — everything a
policy needs, with no trust in souk's summary required.

## Refusing structurally

`CompletionRefused(payload)` raised from a handler travels to the
calling agent as data — souk relays the payload intact and never reads
it. The library defines only the envelope; what a refusal *means*
(a ceiling reached, a chain not served, anything) is vocabulary between
the provider and its callers. Policy itself — spend ceilings, model
allow-lists, chain checks via `verify_chain` — is deliberately the
provider's own few lines, not a library feature; the package README
carries a worked, test-pinned example.
