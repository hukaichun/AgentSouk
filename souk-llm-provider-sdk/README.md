# souk-llm-provider-sdk

What an LLM provider and souk agree on, stated from the provider's side: an
identity and what it signs, the link a transport implements, and the
completion envelope. The peer of `souk-provider-sdk`, for the party that
answers KYOK completion calls with its own key.

**It carries no transport and no policy.** Dependencies are
`souk-provider-sdk` (identity is identity) and `openai` (a completion is an
OpenAI-shaped completion — the types, not the client). Wrapping the link in a
network is a downstream job; deciding what to serve is yours.

## The role

Keep Your Own Key means the agent working your run never holds your LLM
credential. When it needs a completion, it asks souk; souk relays the request
to *your* LLM provider — a process you run, holding your key, spending your
budget. This package is that process's contract surface:

- `ProviderIdentity` / `sign_llm_registration` — who you are, and how you
  register offerings.
- `SoukLLMLink` — the port a transport implements. `InProcessLLMProvider` is
  the in-process transport; a socket is another (in-process is a transport,
  not a special case).
- `DeliveredCompletion` — what your handler receives: the run's identity, the
  caller's opaque `context`, the request body, and the run's verified actor
  chain.
- `souk_provider_sdk.verify_chain` — verify that chain yourself; no trust in
  souk's summary needed.
- `CompletionRefused` — answer with a structured refusal that travels intact
  to the calling agent, instead of an opaque failure.

## Policy is yours — the library guarantees only the interposition point

While a run is live, its agent may request completions as often as it likes,
and every request is billed to you. The library's guarantee is structural:
**every completion passes through your handler before any money moves.**
What to enforce there — ceilings, model allow-lists, chain checks — is
deliberately not the library's contract, so nothing here needs to change
when your policy does. A per-run ceiling is a few lines of your own:

```python
from souk_llm_provider_sdk import CompletionRefused, DeliveredCompletion

MAX_COMPLETIONS_PER_RUN = 20
spent: dict[str, int] = {}

async def guarded(delivered: DeliveredCompletion):
    spent[delivered.run_id] = spent.get(delivered.run_id, 0) + 1
    if spent[delivered.run_id] > MAX_COMPLETIONS_PER_RUN:
        raise CompletionRefused({"kind": "budget-ceiling", "runId": delivered.run_id})
    async for chunk in call_your_model(delivered.body):
        yield chunk
```

The refusal payload's vocabulary (`kind`, retry hints, anything) is between
you and your callers — souk relays it and never interprets it. The same hook
is where a model allow-list (`delivered.body["model"]`) or a delegation-chain
policy (`verify_chain(delivered.actor_chain)`) belongs. Entries in `spent`
should be dropped when a run ends; how you learn that is transport-specific.

## Byte-level contract

`docs/contract-vectors.json` (repo root) publishes the registration payload
bytes and signatures under a test key; this package's tests reproduce them,
and so can an implementation in any language.
