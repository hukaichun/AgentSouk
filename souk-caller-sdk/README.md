# souk-caller-sdk

What a caller and souk agree on, from the caller's side — specifically the
Keep Your Own Key edge, where a provider's LLM call comes back to whoever is
paying for it.

The peer of `souk-provider-sdk`, one relationship over.

## The problem it is the answer to

Running an agent costs LLM tokens. The obvious way to pay is to hand your API
key to whoever hosts the agent, which puts your key on infrastructure you do
not control for as long as they care to keep it.

KYOK inverts that. The provider writes ordinary code that calls "an LLM" — it
just happens to be calling souk, and souk hands the actual completion to you.
This package is your side of that: claim the completion, call your own model
with your own key, stream the answer back.

```python
from souk_caller_sdk import KyokBridge, new_session_id, run_metadata

session_id = new_session_id()

async def my_llm(body):                     # OpenAI-shaped request in,
    async for chunk in call_whatever_i_pay_for(body):   # OpenAI-shaped
        yield chunk                                     # chunks out

bridge = KyokBridge(session_id, my_llm)
serving = asyncio.create_task(bridge.serve_forever(link))

# ...then start the run, offering the session:
await souk_client.run(agent, metadata=run_metadata(session_id))
```

`link` is a `CallerLink` — how you reach souk. `InProcessLink` is the one in
this package; a WebSocket one lives downstream.

## Two absences are the design

**No transport.** No `httpx`, no `websockets`. Wrapping this in a network is a
downstream job, and the empty dependency list is what makes that checkable
rather than a matter of discipline (`tests/test_no_transport_and_no_llm.py`
reads the manifest and every import).

**No LLM client.** No `litellm`, no `openai`. You supply the
`CompletionSource`; which model, which vendor, which key, what it costs and
whether to refuse are yours. A package that chose for you would be making the
single decision KYOK exists to leave you — and it is also the natural place
for a per-run spend ceiling, which only your side can price.

It also names nothing of souk's: completions arrive as `PendingCompletion`,
answers leave through `CallerLink`, and `inprocess.py` is the one module that
knows both sides' words. `contract.py` states the shapes as data so souk's own
suite can check the pair still agrees (`souk/tests/test_caller_sdk_contract.py`).

## Why an in-process carrier exists

Not because anyone deploys one. Because in-process is a transport, not a
special case — the same rule `souk_provider_sdk/inprocess.py` states — and it
is what lets the whole loop be a test: a provider asking for a completion,
your code answering, chunks arriving back, with no gateway, no socket and no
key (`souk/tests/test_kyok_in_process.py`).

Before it, every KYOK test in every repo stopped at a frame.

In-process gets no shortcut a remote bridge does not: the same session
routing, and the same handshake if the bridge side ever gains a credential.

## What is not here

Starting runs, reading threads, browsing the roster. Those are the caller's
*other* relationship with souk, over its public AG-UI/A2A surface. A method
that exists only to mirror souk's API does not belong in this package.

## Status

Experimental, with KYOK itself. This bridge holds no durable state: if the
process dies mid-answer, the completions it was serving fail and the provider
sees errors, with no resume path on either side. See
`docs/keep-your-own-key.md` for the rest of the scope, including what the
bridge session does and does not prove.
