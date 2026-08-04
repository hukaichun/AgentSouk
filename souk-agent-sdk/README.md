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
   - A `souk.run_paused` CUSTOM event followed by `RUN_FINISHED` — see
     [Pausing a run](#pausing-a-run-hitl-and-sub-agent-waits) below.

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

## Pausing a run (HITL and sub-agent waits)

A run doesn't have to finish or fail — it can pause, resumable later,
by emitting one CUSTOM event before ending the stream normally:

```python
yield {
    "type": "CUSTOM",
    "name": "souk.run_paused",
    "value": {},  # or {"waitingOnRunId": <some other run_id>}
}
yield {"type": "RUN_FINISHED", ...}
```

This is deliberately not a new wire protocol — just a regular AG-UI
CUSTOM event (see `souk/pause.py` for the full convention souk-side). A
paused run **never holds the connection open**: end the stream normally
right after, the same as any other completion. souk records
`status='input-required'` instead of `'completed'`.

Two shapes:

- **Plain HITL pause** (`value` has no `waitingOnRunId`): use this when
  your own run needs something only a human can supply (tool-call
  approval, missing information) before it can continue. Someone resumes
  it later with a normal AG-UI/A2A call carrying `resume: true` and
  whatever new `messages` answer what you were waiting on (an approval
  decision, the missing information — there's no special wrapper for
  this, it's just the next ordinary message in the thread). souk handles
  the resume specially even though the caller's request looks ordinary:
  **your run keeps its same `run_id` (and A2A `task_id`, if any) for the
  new round** — `run_stream` gets invoked again with a fresh
  `RunAgentInput` on that same id, not handed off to a new one.

- **Waiting on a specific sub-agent call**
  (`value.waitingOnRunId = <the callee's run_id>`): use this if your run
  genuinely cannot proceed without a specific delegated call's result.
  souk auto-resumes you (no external caller needed) once that run_id
  reaches a real terminal state, with `forwardedProps.resume =
  {"waitingOnThreadId": ..., "status": "completed"|"cancelled"|"failed",
  "result": ...}`. `waitingOnRunId` is deliberately a run_id, not a
  thread_id — the callee's thread may be reused across several separate
  delegation calls over its lifetime, so pinning the specific run
  removes any ambiguity about which call you mean.

  **You usually don't need to do this yourself.** If you delegate via
  `souk_agent_sdk.a2a_client` (see below) and the callee turns out to
  need more time, souk detects this from the callee's real status and
  marks *your currently-active run* as waiting on it automatically —
  see `souk.api_a2a._finalize_delegated_call`. Your tool call just gets
  back an honest "still pending" string instead of a real answer; you
  don't have to emit `souk.run_paused` yourself unless you specifically
  want your own run to stop and wait rather than finishing normally
  with that "pending" answer in hand.

This is single-hop, not transitive: if you delegate further and your
own caller is waiting on you, your caller only gets notified once *your*
run reaches a real terminal state — not on every intermediate pause you
go through. See `souk/pause.py`'s module docstring for the exact
boundary.

## Delegating to another agent (A2A)

`souk_agent_sdk.a2a_client.call_agent_streaming(a2a_url, message,
parent_thread_id=..., actor_chain=...)` drives another agent's
`tasks/sendSubscribe` and yields each status/artifact update as it
streams back. `providers/pydantic-ai-agent/pydantic_ai_agent/
sub_agent_tool.py` is a worked example wiring this up as a pydantic-ai
tool — read it for the full pattern, including:

- Passing `parent_thread_id` (your own run's `threadId`) so souk can
  record lineage (`GET /threads/{root}/tree`) and — per the pause
  section above — automatically mark you as waiting if the callee
  pauses.
- Checking each update's `status.state` honestly: `"failed"` is a real
  failure, `"input-required"` means *pending, not failed and not
  answered* — collapsing that into "no response" or treating it as a
  final answer are both bugs a caller further up will silently act on.
- Forwarding an actor chain (`souk_agent_sdk.identity.new_actor_chain`/
  `extend_actor_chain`) if you want the callee's souk to attribute the
  call to a known, registered identity instead of an anonymous caller.
  Optional — an unattributed call still works.

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
  delegation over A2A, and pause/resume wiring. Start here if you want
  an LLM already wired up.

See `providers/README.md` for how to add a new provider example
alongside these.
