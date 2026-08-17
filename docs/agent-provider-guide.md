# Agent provider guide: situations, how to handle them, what souk does

Two quick starts, for two different questions.
[`souk-provider-sdk/README.md`](../souk-provider-sdk/README.md) is this
repo's: what a provider and souk agree on, and how to serve an agent with
souk as a library in your own process.
[souk-agent-sdk's README](https://github.com/hukaichun/AgentSoukServer/blob/main/souk-agent-sdk/README.md)
(in the AgentSoukServer repo) is the other: running an agent against a
deployed gateway, over a network. They are different packages — see the table
in `souk-provider-sdk/README.md` if the names are running together.

This document is the third thing: the specific situations
a provider design runs into once it's live, how to handle each one, and
exactly what souk does or doesn't do for you. Nothing here is required
reading to get a first agent running — it's what to come back to once
something doesn't behave the way you expected.

## The SDK is convenience, not a requirement

`souk_agent_sdk` (in the AgentSoukServer repo, next to the gateway) is
one Python client for the gateway's provider WebSocket — not the
contract itself. The contract is the frame protocol authored in
AgentSoukServer's `docs/server-mode.md` (`WS /ws/provider`:
hello/welcome, run frames down, event/finish frames up, cancel down);
anything that speaks those frames, in any language — including a browser
— is an equally valid provider; souk never special-cases this SDK. If
you're not on Python, or want to understand exactly what the SDK is
doing for you, read that spec and the SDK's `client.py` side by side.

## Waiting on a sub-agent call that's still pending — this isn't a pause

If your run delegates to another agent via A2A and that call is still in
progress (or itself paused), **don't hold your own run open waiting for
it**, and don't declare any special pause state either — just answer
honestly and finish your own run normally:

```python
pending = False
async for update in call_agent_streaming(a2a_url, message, ...):
    if update.get("status", {}).get("state") == "input-required":
        pending = True  # the callee is still working, or itself paused
if pending:
    return "still waiting on <sub-agent> — you'll get a real answer next time you check"
```

`providers/pydantic-ai-agent/pydantic_ai_agent/sub_agent_tool.py` is the
worked example. There's no subscription to register and nothing to learn
beyond this: whether a *later* call to the same sub-agent gets a real
answer or another "still pending" is decided fresh each time, purely by
whether the callee's own thread can currently accept a new run — not by
anything you declared when you first called it. Souk deliberately has no
"notify me later" mechanism for this: anything it did to wake you up
would tell you nothing you couldn't get by just checking again, and would
cost a real run (and, if you're LLM-backed, a real LLM call) to say it.

**Several concurrent sub-agent calls, one of them pauses while the others
are still running — this is entirely your call to design, not souk's.**
If your framework runs sibling tool calls in parallel (e.g. pydantic-ai's
default `end_strategy`), one call hitting `input-required` doesn't itself
unblock the others still in flight. Whether you want to keep waiting on
them, give up and report "some pending, some still running," or something
else, is a decision about *your own* concurrency model — souk has no
visibility into it and no opinion. This was prototyped inside souk once
and deliberately reverted, precisely because the right answer depends on
your framework's task model, not on anything souk-generic.

## Session continuity across A2A calls is explicit, not automatic

`call_agent_streaming`'s `reference_task_ids` is real A2A
(`Message.referenceTaskIds`) — purely informational lineage, recorded so
`GET /threads/{root}/tree` can show what a top-level call fanned out to.
**It does not make souk reuse or continue any particular thread with the
callee.** Every call that omits `context_id` gets a brand-new thread with
the callee, every time, regardless of `reference_task_ids`.

If you want a sub-agent conversation to continue across multiple calls
(rather than restart cold each time), capture the real `contextId` every
update carries back, and pass it as `context_id` on your next call to
that same callee. Souk never infers continuity on its own — this is by
design, so that recording lineage and continuing a conversation stay two
independent choices you make separately.

## Multi-agent topologies — verified, not just argued

Delegation composes into arbitrary graphs of agents talking to each other
through souk, since each hop is just one more A2A call. Two shapes worth
naming explicitly, both exercised against a real LLM
(`providers/pydantic-ai-agent/config.test-topologies.yaml` — kept in the
repo as a regression fixture, not a live demo):

- **Fan-out / diamond** (you call two sub-agents concurrently, and both
  happen to delegate to the *same* third agent): safe by construction.
  Every call with no `context_id` gets an independent thread, so the
  shared callee ends up with two unrelated threads, one per caller — no
  cross-talk, even though it is the same agent on both branches.
- **Cycles** (agent A delegates to B, and B's own logic calls back into
  A): no *scheduling* deadlock — a callback lands on a fresh thread (no
  `context_id` reuse means it's a brand-new call from souk's point of
  view), and souk's own event loop is never blocked by your `run_stream`
  awaiting an outbound A2A call.

  **Capacity is a different matter, and it is yours to get right.** If a
  provider delegates to an agent *it hosts itself* — including back to the
  same agent — the outer run is holding one of that provider's slots while
  it waits, and the inner run needs a slot from the same provider. With
  `max_concurrent_runs=1` that never happens: souk offers the inner run,
  you decline because you are full, and the outer run sits `running` until
  the stall sweep gives up on it, about two minutes later by default.
  Measured. So a provider that delegates to its own agents needs capacity
  for the whole chain, and one
  that recurses should stay on the default (unlimited). Delegating to a
  *different* provider is unaffected — it has its own budget, and a caller
  capped at 1 is fine.

  **What souk does *not* do: stop you from looping forever.** There's no
  depth limit, no cycle detection — if your own logic unconditionally
  delegates back on every message, you've built
  an infinite loop, and souk will happily keep running it (each hop
  eventually fails on its own `httpx` timeout, but only after real work
  and real LLM spend). Design your own base case, the same way you'd
  design one for any other recursive call — the `looper_a`/`looper_b`
  test agents in the fixture above are the worked example (a message
  prefixed `PING` short-circuits instead of delegating again).

## Pausing and resuming (HITL) — the full round trip

End your stream with a `RUN_FINISHED` whose `outcome` is AG-UI's native
`{"type": "interrupt", "interrupts": [...]}` instead of the default
`{"type": "success"}`. If you're on pydantic-ai,
`Tool(..., requires_approval=True)` does this for you end to end — nothing
here is souk-specific, a provider built this way needs zero souk-aware
code to support pausing.

A paused run never holds the connection open — the `RUN_FINISHED` ends
the stream normally, same as any other completion; souk just records
`status='input-required'` instead of `'completed'`. Someone resumes it
later with a normal AG-UI call carrying `resume: [{"interruptId": ...,
"status": "resolved"|"cancelled", "payload": ...}]` (AG-UI's own
`ResumeEntry`), forwarded to you byte-for-byte — souk never interprets
`payload`. **Your run keeps its same `run_id` (and A2A `task_id`, if any)
for the new round** — `run_stream` is invoked again with a fresh
`RunAgentInput` on that same id, not handed off to a new one.

**Only ever through AG-UI, never through A2A.** A2A structurally cannot
carry a resume — an agent calling you via A2A can never bypass your pause
on a caller's behalf. Whoever needs to actually resolve it must reach you
directly over AG-UI on the same `thread_id`.

## Cancellation: AG-UI has no cancel path today

Souk cancels a run only on an explicit request — and today that's *only*
A2A's `tasks/cancel`, never because an HTTP/SSE caller disconnected.
**AG-UI itself has no cancel endpoint on souk right now.** If every caller
reaching your agent does so purely over AG-UI (never via A2A), your run
is practically un-cancellable from the outside for now — nothing you do
in `run_stream` changes that, since the cancel signal never gets sent in
the first place. Worth knowing rather than assuming cancellation
uniformly "just works" for every caller.

When it does happen (A2A `tasks/cancel`), souk sends a `cancel=true`
envelope on your current gRPC stream — a *request*, not a command: souk
keeps persisting and relaying whatever your run emits afterwards, and if
you finish normally anyway the run is recorded `completed`. The SDK
complies on your behalf: its read loop calls `.cancel()` on the task
running your generator the moment the frame arrives — delivering
`CancelledError` into whatever it's currently awaiting, an in-flight LLM
call included, not just at your next `yield`. `asyncio.CancelledError` is
a `BaseException`, not an `Exception`, since Python 3.8 — an ordinary
`except Exception:` won't swallow it, but if you or a library you call
into ever catches it explicitly, make sure it re-raises.

## Thread/context ids are capability tokens, not public identifiers

`threadId`/`contextId` (souk mints both, database-generated, ~96 bits of
randomness) are the *entire* access boundary for reading a conversation
back — `GET /threads/{thread_id}` and its `/tree` have no separate auth
check; knowing the id is treated as proof you were a party to it, the
same trust model A2A's own `tasks/get` already uses for `run_id`. This is
intentional (souk's own public demo frontend relies on exactly this — an
anonymous browser reads back its own just-finished conversation's lineage
by thread_id alone, with no credential to offer). Practical implication:
**don't log these ids somewhere less trusted than the conversation
itself**, and don't assume there's a souk-side permission check backing
you up if you hand one to the wrong party — there isn't.

## Identity and registration

A provider's identity to any souk it connects to is its Ed25519 keypair —
not an account souk issues, and an agent *is* `(your public key, its name)`
— there is no separate id souk mints for you to hold. Losing the key file
therefore means losing the agents: a regenerated key registers as a fresh,
unrelated identity, and anything pointed at the old pair (another
provider's sub-agent delegation config, say) keeps talking to the orphaned
one. Treat the key file like any
other credential — back it up, never commit it.

## TLS

Both the gRPC and HTTP sides support TLS, and **neither is on by
default** — plaintext is fine same-host (e.g. `docker compose up`), not
for anything reachable over a real network. Pass
`SoukProvider(ca_cert_path=...)` to verify the souk you're connecting
to against a specific CA/self-signed cert rather than the system trust
store — this is what actually confirms you're talking to *this* souk and
not an impostor on the network; skipping it means you can't tell the
difference. `scripts/gen_dev_tls_cert.py` generates a self-signed pair for
local testing; see the top-level README's Security section for the
server-side settings this pairs with.

## Keep Your Own Key (KYOK) — experimental

Treat this as experimental, unlike everything else in this document:
`souk/api_llm_bridge.py`/`souk/kyok.py` have no test coverage today
(contrast the broker/pause/health paths, which have been through several
rounds of race-condition review), and the bridge's pending-completion
registry is in-memory/single-process only. Fine for a demo or low-stakes
integration; if a run genuinely dies mid-relay (souk restart, provider
crash) there's no persisted state to recover from, and that path hasn't
been exercised under test. See
[`docs/keep-your-own-key.md`](keep-your-own-key.md) for the full design.
