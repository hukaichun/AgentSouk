# souk-agent-sdk: what an agent provider needs to do

This is the contract a "provider" (an actual agent, running wherever you
run it) has to satisfy to be reachable through a souk. `souk_agent_sdk` is
a convenience Python client for this contract — it is not the contract
itself. Anything that speaks `proto/souk.proto`'s gRPC service directly,
in any language, is an equally valid provider; souk never special-cases
this SDK.

If you just want a working example to copy, skip to
[Reference implementations](#reference-implementations). This document is
for understanding *why* each piece is required.

## The actual pitch: your agent probably already qualifies

souk's headline feature (see the top-level README) is making agents
reachable — over **AG-UI** and **A2A** — without exposing any inbound
port, tunnel, or public IP: the agent connects *out* to souk, souk does
the rest. The part that matters for you here is what "becoming reachable"
actually costs.

Look again at the contract above: `run_stream(run_input)` takes a real
AG-UI `RunAgentInput` and yields real AG-UI events. That's not a
souk-specific shape layered on top of AG-UI — it *is* AG-UI. If you've
already built an agent that speaks AG-UI (CopilotKit's `ag-ui-protocol`,
your own hand-rolled event stream, whatever framework already emits
`TEXT_MESSAGE_*`/`RUN_STARTED`/`RUN_FINISHED`), you have already written
the one function this whole SDK exists to wrap. There is no separate
"souk agent" you build — you take the agent you have, put its existing
AG-UI-shaped streaming loop behind `AgentHandle(run_stream=...)`, and it's
now a provider, reachable from anywhere, with zero inbound network
exposure and no rewrite of your actual agent logic. Everything else this
document describes (registration, polling, gRPC transport, cancellation,
pause/resume, KYOK) is infrastructure *around* that one function — souk
never asks you to change how your agent thinks or talks, only how it gets
plugged in.

## Why use this instead of talking gRPC directly

Nothing stops you from implementing `proto/souk.proto` yourself — souk
doesn't care. What `SoukAgentClient` buys you, in exchange for writing one
`run_stream` function, is everything *around* that function that's easy to
get subtly wrong if you write it by hand:

- **Reconnection and token refresh are not your problem.** `run_forever()`
  re-registers on every (re)connect and refreshes the gRPC bearer token
  before it expires as a side effect of that — there's no separate timer
  to manage, and no "the connection silently died three hours ago and
  nobody's polling anymore" failure mode to debug.
- **Cancellation actually cancels.** `_handle_run` races your `run_stream`
  against souk's cancel signal and calls `.cancel()` on the real asyncio
  task the moment it arrives — including into an in-flight LLM call, not
  just at your next `yield`. Rolling this yourself means either polling
  for cancellation manually inside every long-running step, or not really
  supporting cancellation at all.
- **Backpressure is a constructor argument.** `max_concurrent_runs=N` caps
  how much work this process claims across all its agents; get this
  wrong by hand and you either leave capacity idle or take on more
  concurrent runs than your real LLM rate limit / GPU / whatever can
  actually serve.
- **Pausing and A2A delegation are a couple of function calls, not a
  protocol you design.** A native AG-UI interrupt outcome pauses a run
  resumably; `a2a_client.call_agent_streaming` drives a full
  `tasks/sendSubscribe` exchange — see [Pausing a run](#pausing-a-run-hitl)
  and [Delegating to another agent](#delegating-to-another-agent-a2a)
  below.
- **Identity is a keypair the SDK manages for you**, not an account you
  provision through some other channel — `load_or_create_identity`
  generates and persists it, and the same key is what makes A2A
  delegation and [KYOK](#keep-your-own-key-kyok-opt-in) attribution work
  without extra setup.
- **Opting into [KYOK](#keep-your-own-key-kyok-opt-in) costs one
  constructor argument on your `httpx.Client`**, reusing the identity key
  you already have — not a second SDK or a different wire protocol.

None of this is large in isolation, but it's the kind of code that's easy
to get almost-right and hard to notice is almost-right until a run hangs,
a cancel doesn't propagate, or a token expires mid-stream. That's the
actual trade you're making by depending on this package instead of the
raw proto.

## The contract, minimally

A provider is one or more named agents, each backed by a function:

```python
RunStream = Callable[[dict], AsyncIterator[dict]]
```

`run_stream(run_input)` takes a real AG-UI `RunAgentInput` (a plain dict,
camelCase wire keys — `threadId`, `runId`, `messages`, `state`, `tools`,
`context`, `forwardedProps`) and yields AG-UI event dicts. That's the
entire interface. Everything else — registering with souk, long-polling
for work, opening/maintaining the gRPC stream, reconnecting after a
network blip — is `souk_agent_sdk.SoukAgentClient`'s job, not yours.

```python
from souk_agent_sdk import AgentHandle, SoukAgentClient

client = SoukAgentClient(
    souk_http_url, souk_grpc_url,
    agents=[AgentHandle(name="my-agent", run_stream=run_stream)],
)
await client.run_forever()
```

See `/agent-template/agent_template/main.py` for the smallest possible
`run_stream` (echoes the last user message, no LLM) — start there to see
the exact event sequence, or copy it as the seed for a provider written
from scratch.

## Minimum viable event sequence

Every run must yield, in order:

1. `{"type": "RUN_STARTED", "threadId": ..., "runId": ...}`
2. Zero or more content events. For plain text, that's
   `TEXT_MESSAGE_START` → one or more `TEXT_MESSAGE_CONTENT` (`delta`) →
   `TEXT_MESSAGE_END`, each carrying the same `messageId`. Tool calls,
   state deltas, and other AG-UI event types are also legal here; souk
   persists and relays whatever you yield without interpreting most of
   it.
3. Exactly one of:
   - `{"type": "RUN_FINISHED", ...}` — the run completed normally.
   - `{"type": "RUN_ERROR", "message": "..."}` — the run failed; souk
     records this and marks the run `failed`.
   - `{"type": "RUN_FINISHED", "outcome": {"type": "interrupt", ...}}`
     — the run paused, resumable later — see
     [Pausing a run](#pausing-a-run-hitl) below.

`run_stream` returning (the generator ending) without ever yielding
`RUN_FINISHED`/`RUN_ERROR` is not itself an error souk detects — do not
rely on this; always emit an explicit terminal event.

## Registration and identity

A provider's identity to any souk it connects to is its Ed25519 keypair
— not an account souk issues. `SoukAgentClient(identity_key_path=...)`
generates one on first run and persists it to disk
(`souk_agent_sdk.identity.load_or_create_identity`). This matters
because:

- `/agents/register` requires a signature proving possession of the
  private key — an `agent_id` is assigned once per `(public_key, name)`
  pair and stays owned by whoever holds that key.
- **Losing this file means losing the ability to update that
  registration under its original `agent_id`.** A regenerated key is a
  fresh, unrelated identity: souk lets it register under the same
  `name` again (names aren't exclusive), but hands out a *new*
  `agent_id`. Anything still pointed at the old `agent_id` — e.g.
  another provider's sub-agent delegation config using
  `/a2a/id/{agent_id}/rpc` — keeps talking to the orphaned identity, not
  this one. Treat the key file like any other credential: back it up,
  never commit it.
- `run_forever()` re-registers on every (re)connect, not just the
  first. This is also how the bearer token used on every gRPC call gets
  refreshed before it expires — there's no separate renewal mechanism.

## Cancellation

Souk cancels a run only on an explicit request (A2A `tasks/cancel`) —
never because an HTTP/SSE caller disconnected (see `souk/broker.py`'s
module docstring for why: a dropped connection alone must not throw
away in-progress work). When it does happen, souk sends a `cancel=true`
envelope on the run's gRPC stream.

The SDK's `_handle_run` races your `run_stream` against watching for
that envelope and, if it arrives first, calls `.cancel()` on the task
actually running your generator — delivering `CancelledError` into
whatever it's currently awaiting (an in-flight LLM call included), not
just at the next `yield`. `asyncio.CancelledError` is a `BaseException`,
not an `Exception`, since Python 3.8 — an ordinary `except Exception:`
inside `run_stream` won't swallow it. But if you (or a library you call
into) ever catch it explicitly, or use a bare `except:`, make sure you
re-raise: swallowing it means your run keeps burning LLM/tool calls
nobody is waiting on anymore.

## Pausing a run (HITL)

A run doesn't have to finish or fail — it can pause, resumable later, if
it genuinely cannot make further progress without something only a
human/caller can supply (tool-call approval, missing information). This
is AG-UI's own mechanism, not souk's — see `souk/pause.py` for the full
convention souk-side.

Use this when your own run needs that kind of input before it can
continue. End your stream with a `RUN_FINISHED` whose `outcome` is
AG-UI's native `{"type": "interrupt", "interrupts": [...]}`
(`ag_ui.core.RunFinishedInterruptOutcome`/`Interrupt`, ag-ui-protocol
>= 0.1.19) instead of the default `{"type": "success"}`:

```python
yield {
    "type": "RUN_FINISHED",
    "threadId": ..., "runId": ...,
    "outcome": {
        "type": "interrupt",
        "interrupts": [{"id": "...", "reason": "...", "message": "..."}],
    },
}
```

**If you're on pydantic-ai, you likely don't need to build this by
hand at all**: `Tool(..., requires_approval=True)` makes pydantic-ai's
own AG-UI adapter emit and consume this outcome for you, end to end —
see `providers/pydantic-ai-agent`'s tool definitions for where that
flag goes. Nothing here is souk-specific; a provider built this way
needs zero souk-aware code to support pausing.

A paused run **never holds the connection open**: this `RUN_FINISHED`
ends the stream normally, same as any other completion. souk records
`status='input-required'` instead of `'completed'`, with the
interrupts preserved. Someone resumes it later with a normal AG-UI
call carrying `resume: [{"interruptId": ..., "status":
"resolved"|"cancelled", "payload": ...}]` — also AG-UI's own field
(`ag_ui.core.ResumeEntry`), forwarded to you byte-for-byte; souk never
interprets `payload`. **Your run keeps its same `run_id` (and A2A
`task_id`, if any) for the new round** — `run_stream` gets invoked
again with a fresh `RunAgentInput` on that same id, not handed off to
a new one.

### Waiting on a specific sub-agent call — this isn't a pause at all

If your run delegated to another agent and that call is still in
progress (or itself paused), don't hold your own run open waiting for
it, and don't declare any special pause state either — just answer
honestly and finish normally:

```python
pending = False
async for update in call_agent_streaming(a2a_url, message, ...):  # a2a_client
    if update.get("status", {}).get("state") == "input-required":
        pending = True  # the callee is still working, or itself paused
if pending:
    return "still waiting on <sub-agent> — you'll get a real answer next time you check"
```

`providers/pydantic-ai-agent/pydantic_ai_agent/sub_agent_tool.py` is the
worked example. There's no subscription to register and nothing to
learn beyond this: whether a *later* call to the same sub-agent gets a
real answer or another "still pending" is decided fresh each time,
purely by whether the callee's own thread can currently accept a new
run (see `souk.repo.get_active_run_for_thread` — the same check that
guards every ordinary call) — not by anything you declared when you
first called it. You (or whoever prompts your agent next — a new
message, your own next turn) simply ask again whenever there's reason
to; souk has no separate "notify me later" mechanism for this, on
purpose — anything souk did wake you up for would tell you nothing you
couldn't get by just checking, and would cost a real run (and, if
you're LLM-backed, a real LLM call) to say it.

## Delegating to another agent (A2A)

`souk_agent_sdk.a2a_client.call_agent_streaming(a2a_url, message,
parent_thread_id=..., actor_chain=...)` drives another agent's
`tasks/sendSubscribe` and yields each status/artifact update as it
streams back. `providers/pydantic-ai-agent/pydantic_ai_agent/
sub_agent_tool.py` is a worked example wiring this up as a pydantic-ai
tool — read it for the full pattern, including:

- Passing `parent_thread_id` (your own run's `threadId`) so souk can
  record lineage (`GET /threads/{root}/tree`) — lets whoever started the
  original call later trace what it fanned out to.
- Checking each update's `status.state` honestly: `"failed"` is a real
  failure, `"input-required"` means *pending, not failed and not
  answered* — collapsing that into "no response" or treating it as a
  final answer are both bugs a caller further up will silently act on.
- Forwarding an actor chain (`souk_agent_sdk.identity.new_actor_chain`/
  `extend_actor_chain`) if you want the callee's souk to attribute the
  call to a known, registered identity instead of an anonymous caller.
  Optional — an unattributed call still works.

## Keep Your Own Key (KYOK) opt-in

By default your agent calls whatever LLM you configure it to call, paid
for however you normally pay for it. KYOK lets a *caller* offer to pay for
a given run with their own key instead — see
[docs/keep-your-own-key.md](../docs/keep-your-own-key.md) for the full
design; this section is just the provider-side integration cost, which is
small and entirely opt-in per agent.

If a run's `forwardedProps.kyok.token` is present, souk is offering you a
run-scoped OpenAI-compatible endpoint (`{souk_http_url}/kyok/v1`) instead
of your own LLM config — point an `OpenAIChatModel`/`OpenAIProvider` at it
with that token as the API key, same as any other OpenAI-compatible host.
The one thing beyond ordinary OpenAI-wire-compatibility: souk verifies
*who* is actually calling, live, on every request — so build your
`httpx.Client`/`AsyncClient` with `auth=KyokSigningAuth(signing_key)`,
reusing the same Ed25519 key `load_or_create_identity` already gave you
for registration:

```python
from souk_agent_sdk import KyokSigningAuth

http_client = httpx.AsyncClient(auth=KyokSigningAuth(signing_key))
provider = OpenAIProvider(
    base_url=f"{souk_http_url}/kyok/v1", api_key=kyok_token, http_client=http_client,
)
```

That's the entire integration: no new keypair, no change to how you build
the request body, no per-provider LLM translation code (the caller's own
bridge is what normalizes across real LLM providers, via litellm — not
something you need to think about). `providers/pydantic-ai-agent`'s
`resolve_kyok_model` is the full ~15-line version of this, including how
to fall back to your own configured model when a run carries no KYOK
offer at all.

## Concurrency

`SoukAgentClient(max_concurrent_runs=N)` caps how many runs this
provider claims at once, across all its agents combined — set this to
your real capacity so souk leaves the rest queued instead of handing you
more than you can process. Leaving it `None` (the default) claims souk's
entire backlog for this provider on every poll — fine for a demo, not
for anything with real concurrency limits (a bounded LLM API rate,
limited local GPU, etc.).

## TLS

Both the gRPC and HTTP sides support TLS, and **neither is on by
default** — plaintext is fine same-host (e.g. `docker compose up`), not
for anything reachable over a real network. Pass
`SoukAgentClient(ca_cert_path=...)` to verify the souk you're connecting
to against a specific CA/self-signed cert rather than the system trust
store — this is what actually confirms you're talking to *this* souk
and not an impostor on the network; skipping it means you can't tell
the difference. `scripts/gen_dev_tls_cert.py` generates a self-signed
pair for local testing; see the top-level README's Security section for
the server-side settings this pairs with.

## Reference implementations

- **`/agent-template`** (repo root) — the floor: the smallest possible
  `AgentHandle` implementation, no LLM, no framework. Copy this as the
  starting point for a provider written from scratch, or to see the raw
  event sequence with nothing else going on.
- **`providers/pydantic-ai-agent/`** — the ceiling of what's demonstrated
  here: YAML-configured agents backed by
  [pydantic-ai](https://ai.pydantic.dev), MCP tool support, sub-agent
  delegation over A2A, pause/resume wiring, and per-agent
  [KYOK](#keep-your-own-key-kyok-opt-in) opt-in (`use_kyok: true` in
  `config.yaml`). Start here if you want an LLM already wired up.

See `providers/README.md` for how to add a new provider example
alongside these.
