# Library architecture: souk as a network-free core

Status: **design, not yet implemented.** Supersedes the implicit structure
of today's `souk/` subproject, which is a server that happens to be
pip-installable.

## The principle

> souk core is network-agnostic. At most it knows about a database.

Three layers get conflated in the current code, and the cut belongs
between the second and the third:

| Layer | What it is | Belongs to souk? |
|---|---|---|
| ① **Domain** | agents, threads, runs, events, providers | ✅ core |
| ② **Protocol translation** | AG-UI event shapes, A2A JSON-RPC semantics — pure translation, no I/O | ✅ core |
| ③ **Serving** | uvicorn, binding ports, TLS, `grpc.aio.server()` | ❌ never |

② is core, not an optional extra, for the same reason `pydantic-ai` ships
`AGUIAdapter` in the library rather than as a plugin: the protocol mapping
*is* the semantic model, and it must be usable in-process without a socket.
A caller that wants A2A task semantics against a local agent should not have
to stand up an HTTP server to get them.

③ is never souk's, because the moment souk calls `uvicorn.run()` or
`add_insecure_port()` it has decided policy on behalf of whoever hosts it —
which framework, which middleware, which port, which TLS story. souk hands
back objects and pure functions; the host decides how (or whether) they
reach a network. This is the same mechanism/policy split the project already
applies to registration (see `docs/federation-and-anti-abuse.md`).

## Package layout

```
souk/                 # the library. Network-free.
  core/               #   ① Souk class, broker, handlers, repo, schema
  providers/          #   AgentProvider port (the AG-UI agent shape)
  protocols/          #   ② agui, a2a — pure translation, in-process usable
souk-server/          # ③ separate subproject: the reference gateway
  http/               #   FastAPI/uvicorn wiring of souk.protocols
  relay/              #   gRPC servicer + GrpcProvider (NAT-traversal relay)
```

`souk`'s only hard dependency is SQLAlchemy (plus a driver). It must not
import `fastapi`, `uvicorn`, or `grpcio` — that's the invariant this whole
design exists to protect, and it should be enforced by a test.

`souk-server` is a sibling subproject like `souk-agent-sdk` and
`souk-client-sdk`, not an extra of `souk`. A separate distribution makes the
boundary impossible to erode by accident: core cannot grow a `uvicorn`
import without someone noticing they're in the wrong package.

## The core object

```python
from souk import Souk, Settings

souk = Souk(Settings(database_url="sqlite+aiosqlite:///./souk.db"))
await souk.start()          # orphan cleanup + health sweeps
```

`Settings` is passed in, not read from the environment at import time. The
engine and sessionmaker belong to the instance, so several souks can coexist
in one process (multi-tenant hosts, and tests that no longer juggle
`os.environ` before imports).

Passing settings *adds* a way to configure souk; it does not remove the
existing one. `Settings` is still a `pydantic-settings` model, so any field
not passed explicitly is still read from its `SOUK_*` environment variable.
What changes is *when* that resolution happens: at the `Souk(...)` call
rather than as a side effect of importing `souk.config`.

### Configuration, split by layer

Today's `Settings` holds nineteen fields covering everything from timeouts to
TLS paths. They do not all belong in a network-free core, so the class splits
along the same line as the packages:

**Core** — the database, the domain's own timing policy, and the signing key:

| Field | What it controls |
|---|---|
| `database_url` | which database to talk to |
| `db_schema` | Postgres schema namespace; ignored on SQLite |
| `online_window_seconds` | how recently an agent must have checked in to count as online |
| `stale_hidden_window_seconds` | when an agent drops off the roster entirely, not just shows offline |
| `queued_timeout_seconds` | how long a run may sit unclaimed before souk gives up on it |
| `run_stall_timeout_seconds` | how long a claimed run may go silent before it is presumed stalled |
| `paused_timeout_seconds` | how long an `input-required` run may wait on a human (`None` = forever) |
| `health_sweep_interval_seconds` | how often the sweeps above run |
| `token_signing_secret` | signs session tokens (`identity.py`) and KYOK HMACs (`kyok.py`) |

None of these describe a network. The signing secret is the one arguable
case, and it stays in core because issuing a token is part of registering an
agent — a domain act — not part of serving a port.

**`souk-server`** — every field that only means something once there is a
socket: `http_host`, `http_port`, `grpc_host`, `grpc_port`,
`cors_allow_origins`, and the four `*_tls_cert_path` / `*_tls_key_path`
values.

**`public_http_url` is the one genuine boundary case.** It looks like
serving (it is a URL) but is used as *content*: `api_a2a` interpolates it to
build the URLs advertised in an Agent Card. It belongs to neither side as a
setting — core should not know what it is called on a network — so it becomes
an argument the server passes when constructing the protocol adapter:

```python
A2AAdapter(souk, public_base_url="https://souk.example.com")
```

### Attaching agents

```python
souk.attach_provider("my-agent", pydantic_ai_agent.to_ag_ui())
```

An agent reaches souk through an `AgentProvider`, and that port is *the AG-UI
agent shape* — input in, event stream out — not an interface of souk's own
invention:

```python
class AgentProvider(Protocol):
    def run(self, run_input: RunAgentInput) -> AsyncIterator[AgentEvent]: ...
```

An earlier draft of this document had four methods here (`deliver_input`,
`signal_cancel`, `ack`) plus a `RunInbox` return-path interface for the
provider to push events back through. That was souk inventing a protocol
alongside one that already exists: AG-UI already defines an agent as
`RunAgentInput → stream of events`, which is exactly what
`pydantic_ai.ui.ag_ui.AGUIAdapter` produces. Inventing a parallel push
interface is precisely what `souk-no-forced-protocol-deviation` exists to
prevent, so the port collapses to the standard shape:

| earlier draft | AG-UI-aligned |
|---|---|
| `deliver_input(inbox, run_input)` | call `run(run_input)` |
| `inbox.relay_event(e)` | `yield e` |
| `inbox.finish()` | the iterator ends |
| `signal_cancel(run_id)` | `aclose()` on the iterator |
| `ack(run_id)` | gone from the port — see below |
| `RunInbox` | does not exist |

Two consequences worth stating plainly, because they are costs, not free wins:

- **`ack` leaves the port.** It exists so an SDK knows souk durably persisted
  *and* relayed everything for a run — a wire concern with no meaning
  in-process. It becomes an internal detail of the gRPC implementation, which
  is where it belongs.
- **The gRPC provider does its own per-run demultiplexing.** `AgentSession` is
  one multiplexed connection carrying every run that client claimed, so
  turning that back into one iterator per run needs an internal queue. That
  complexity does not disappear; it moves out of core and into the transport.
  It is also close to what `AgentSession` already does today with its
  `outbound` queue and `handle_incoming` demux loop.

Claiming still happens the same way (`PollForWork`, then a claim frame); what
changes is only that souk hands the claimed run's `RunAgentInput` to
`provider.run(...)` and consumes what comes back, instead of pushing
protobuf envelopes onto a queue.

This port is what removes the one genuine transport leak in today's code.
`souk/broker.py` is already transport-agnostic (plain asyncio, commands are
pure data), and four of the five handlers in `grpc_server.py` are pure domain
logic. The leak is that `_handle_claim`, `_handle_finish` and `_handle_cancel`
construct `souk_pb2.AgentEventEnvelope` protobuf messages directly onto
`Run.agent_outbound`. Replacing that queue with an `AgentProvider` moves all
five handlers into core unchanged in substance, and leaves protobuf
serialization to the one implementation that needs it.

Because `AgentProvider` is a structural `Protocol` matching the AG-UI agent
shape, **an in-process agent needs no souk-specific wrapper at all** — anything
with `run(RunAgentInput) -> AsyncIterator[...]` already satisfies it, which is
what `pydantic_ai_agent.to_ag_ui()` returns. souk ships no `InProcessProvider`
adapter class because there is nothing for it to adapt.

That leaves one real implementation: **`GrpcProvider`** (in `souk-server`),
which presents the relay's multiplexed `AgentSession` stream as one iterator
per run, and owns the `ack` framing. Both paths go through the same broker
machinery, so claim races and cancellation semantics are implemented once
rather than per transport.

### Typed data, and where typing stops

souk's own data is modeled, not passed around as bare `dict`. Today it is
not, and the current code shows why that is a wart rather than a choice:
`agui.build_run_agent_input` validates its argument into a real
`ag_ui.core.RunAgentInput` and then immediately calls `.model_dump()`,
discarding the model at the boundary. Everything downstream is left poking
strings — `event.get("type")` in `agui_reduce`, `outcome.get("type")` in
`pause`. The types already exist in `ag-ui-protocol`; core should keep them.

So: **anything souk constructs or owns is a pydantic model** — `RunAgentInput`
on the way to a provider, and souk's own agent/thread/run state on the way
back out of the query methods.

The relayed **event stream** is where that stops, and the reason is specific
rather than general caution. Measured against the installed `ag-ui-protocol`:

| behaviour | result |
|---|---|
| unknown event `type` (a newer AG-UI's new event) | **rejected** — `type` is an `EventType` enum, and the `Event` union is discriminated on it: *"Input tag 'SOME_FUTURE_EVENT' … does not match any of the expected tags"* |
| unknown *field* on a known event type | **preserved** — `model_config` is `extra='allow'`; a `futureField` survives a validate/dump round trip intact |
| validate then dump, default settings | **adds** `timestamp: null` and `rawEvent: null` to the payload |
| validate then dump with `exclude_none=True` | **byte-identical** to the input |

So the risk is narrower than "reparsing rewrites things". Unknown *fields* are
safe, and the null-injection is avoidable. souk also already round-trips every
event through a `dict` today (`_handle_relay` does `json.loads` and forwards
the parsed object, never the original bytes), so parse-and-reserialize is not
a new hazard being introduced.

The one real hazard is the first row: souk is a relay, and a provider running
a newer AG-UI than souk must not have its run broken because souk refuses an
event type it has not heard of. `RawEvent` cannot paper over this — its `type`
is a hard-coded `Literal[EventType.RAW]`, so wrapping an unrecognized event in
one changes what the caller sees from the real new event type to `RAW`. That
is not faithful relaying; it is quiet corruption, and worse than passing the
event through untouched.

Hence the port's event type admits both:

```python
AgentEvent = BaseEvent | dict
```

Typed when the provider produces typed events — an in-process pydantic-ai
adapter naturally does — and the original mapping when it is an event this
souk does not recognize. Two rules follow:

1. Whenever souk dumps a typed event, it uses `exclude_none=True`, so it never
   injects `timestamp: null` / `rawEvent: null` into a caller's stream.
2. souk reads only the fields it actually decides on (`type`, and `outcome`
   for pause detection — see `pause.interrupt_outcome_of`), in a way that
   works for both forms.

### Running and querying

```python
handle = await souk.start_run("my-agent", messages=[...], thread_id=None)
handle.run_id
handle.thread_id
handle.is_live            # False: nothing live to consume, see below
async for event in handle.events():
    ...
await handle.cancel()
```

`start_run` returns a `RunHandle` rather than a bare async iterator, because
a run has to be addressable three different ways at once and an iterator
only covers one of them:

- **Streaming** — AG-UI, and A2A's `tasks/sendSubscribe`, consume events as
  they arrive.
- **Collect-and-return** — A2A's `tasks/send` drains the whole run and
  answers with one `Task` object. Not streaming, but still needs every event.
- **Address it later by id** — A2A's `tasks/get` and `tasks/cancel` come back
  to a run long after the call that started it. `Task.id` *is* the `run_id`,
  so the id must be available without consuming (or even starting) a stream.

`is_live` carries a distinction the current code expresses as a local
`is_new` flag in `api_a2a._rpc`: a call may resolve to a run that has nothing
live to consume — already paused, already finished, or fast-failed because
the target agent was offline. There is no in-memory run to drain in that
case, and the answer has to be reconstructed from persisted state instead.
Both A2A methods branch on this today, and they branch *differently*
(`tasks/send` replays the stored events; `tasks/sendSubscribe` emits a single
status update and closes), so the handle exposes the condition rather than
papering over it.

State queries, which is what makes souk usable as an embedded component
rather than only as a relay:

```python
await souk.list_agents()
await souk.get_agent(agent_id)

await souk.get_thread(thread_id)
await souk.get_thread_messages(thread_id)
await souk.get_thread_tree(thread_id)        # multi-hop delegation lineage

await souk.get_run(run_id)
await souk.resume_run(run_id, input)          # HITL: same run_id across rounds
souk.active_runs()                            # live in-memory broker state
```

## What this leaves open: horizontal scaling

Not a goal now, but the layering should not foreclose it. Where souk's state
lives today:

| Durable, already shared | Live, per-process |
|---|---|
| agent roster | `RunBroker._runs`, `_pending_by_agent` |
| threads, thread_history | each `Run`'s `in_queue` / `out_queue` |
| run status, `run_events` | `_wake_subscribers` (long-poll wakeups) |
| | the KYOK bridge registry (same shape) |
| | the `AgentSession` gRPC connection itself |

That split is deliberate: the database is the durable record, explicitly *not*
on the live event-relay hot path, so dispatch runs on plain asyncio primitives
(see `broker.py`'s module docstring).

The real obstacle to multiple replicas is not the queue, it is **connection
affinity**: a provider's `AgentSession` is pinned to one process. If node A
holds agent X's stream and a run for X is created on node B, B cannot dispatch
it. The SSE side has the mirror problem — events are produced wherever
`out_queue` lives.

The `AgentProvider` port is exactly the seam that problem needs. Forwarding to
whichever node holds the connection becomes another implementation of it —
core does not care whether a provider is in-process, a gRPC-connected agent,
or a peer node:

```python
class RemoteProvider:                  # forwards to the node holding the stream
    def run(self, run_input): ...      # same port, core unchanged
```

Two things would still be required, and neither is built here:

1. **`RunBroker` behind an interface**, so a Postgres `SKIP LOCKED` or Redis
   implementation can substitute. This design does the cheap half now: the
   broker becomes an instance owned by a `Souk` rather than a module-level
   singleton, injected like settings and the engine. `broker.py` already
   anticipated this — its wake mechanism is deliberately a plain
   `asyncio.Event` and documents itself as a swap-in seam.
2. **Cross-node fan-out for `out_queue`**, so an SSE consumer on one node can
   read a run produced on another.

Building either now would be premature. Leaving the door shut would not.

## Protocol adapters (core)

Pure translation. No framework, no transport:

```python
from souk.protocols.a2a import A2AAdapter
from souk.protocols.agui import AGUIAdapter

a2a = A2AAdapter(souk)
# Request arrives from the wire, so it is parsed leniently; the response is
# souk's own construction, so it is a model the host serializes.
response: JSONRPCResponse = await a2a.handle_rpc(payload)

agui = AGUIAdapter(souk)
async for event in agui.run(agent_id, run_input: RunAgentInput):
    ...      # relayed provider events, forwarded as-is (see above)
```

Note what is *not* in these signatures: no `Request`, no `Response`, no
`EventSourceResponse`. An adapter never touches a framework type — that is
the line that keeps ② out of ③. Serving them is a few lines in whatever
framework the host already uses. Keeping them in core is what preserves the hard-won mapping
decisions that would otherwise be re-derived (incorrectly) by every
integrator:

- A2A `Task.id` == souk `run_id`, stable across pause/resume rounds
- A2A `contextId` == souk `thread_id`
- `referenceTaskIds` records lineage only; it never groups sessions
- an unrecognized AG-UI `threadId` mints a real thread rather than 404ing

See `souk-no-forced-protocol-deviation`: these exist so a standard AG-UI or
A2A client never has to deviate from its own spec to talk to souk.

## What `souk-server` is

The reference gateway, assembled from the above and owning every I/O
decision: reads `SOUK_*` env vars into `Settings`, applies CORS, binds the
HTTP port, binds the gRPC port, terminates TLS, runs the relay. Behavior
identical to today's `souk-server` console script.

It is also where the gRPC relay lives — outbound-only NAT traversal is
souk's headline capability, but architecturally it is a transport, so it
ships as an `AgentProvider` implementation in the server subproject rather
than in core.

## Migration

Ordered so each step lands green:

1. **Settings injection.** `Settings` passed to a `Souk` instance; engine and
   sessionmaker become instance state. Touches the ten modules that read the
   global `settings` today.
2. **Provider port.** Introduce `AgentProvider`; replace `Run.agent_outbound`
   with it; move the five handlers into core.
3. **`Souk` facade.** Domain methods over repo + broker, including the state
   queries above.
4. **Protocols.** Extract AG-UI and A2A translation out of `api_agui.py` /
   `api_a2a.py` into `souk.protocols`, as pure functions.
5. **Split `souk-server`.** Move HTTP endpoints, gRPC servicer/provider, KYOK
   endpoints and process bootstrap into the new subproject. Add the test that
   fails if core imports fastapi/uvicorn/grpcio.

## What breaks

Everything below is intentional; souk is unreleased at 0.1.0, which is the
cheapest possible moment to make these changes.

- `from souk.server import app` disappears. No compatibility shim.
- `souk.config.settings` as an import-time global disappears.
- The `souk-server` console script moves to the new subproject.
- Providers and clients are unaffected: the gRPC wire protocol
  (`proto/souk.proto`) and the HTTP surface are unchanged, so
  `souk-agent-sdk`, `souk-client-sdk` and `souk-directory` keep working
  against a running `souk-server`.

## KYOK splits the same way, for a structural reason

KYOK (`souk/kyok.py`, `api_llm_bridge.py`) lets a caller keep its own LLM
credentials: the caller's own bridge is what actually calls the model, so the
caller decides the key *and* the model/endpoint, while the provider only ever
sees an OpenAI-compatible URL.

Mechanically it is a **second broker**, structurally identical to the run
broker that already lives in core:

| KYOK | run equivalent |
|---|---|
| `GET /kyok/poll` | `PollForWork` — long-poll for work |
| `POST /kyok/v1/chat/completions` | submitting a unit of work |
| `POST /kyok/respond/{id}` | `AgentSession` — streaming the result back |
| `_DONE` sentinel | `END_OF_STREAM` |
| its local queued timeout | `queued_timeout_seconds` |

So it splits exactly like runs do, and gets its own port:

```python
class LLMBridge(Protocol):
    """Whoever answers a completion request — the caller's own bridge."""
    async def complete(self, request: CompletionRequest) -> AsyncIterator[Chunk]: ...
```

- **in-process** — call a local LLM client directly; no HTTP anywhere
- **HTTP** (`souk-server`) — today's three `/kyok/*` endpoints

Token minting/verifying (`kyok.py`) is core: it signs with
`token_signing_secret`, which core already holds, and the `agent_id` it
carries is checked against the broker's live view of who is running that
`run_id` right now — a domain judgement, not a transport concern.
