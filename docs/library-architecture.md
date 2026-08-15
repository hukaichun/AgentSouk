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
await souk.attach_provider(agent_id, my_agent)
```

An agent reaches souk through an `AgentProvider`, which is *the AG-UI agent
shape* — a run input in, a stream of events out — not an interface of souk's
own invention:

```python
class AgentProvider(Protocol):
    async def start(self, run_input: dict) -> AsyncIterator[AgentEvent]: ...
    async def cancel(self, run_id: str) -> None: ...
```

An earlier draft had four methods plus a `RunInbox` return-path interface for
providers to push events back through. That was souk inventing a protocol
alongside one that already exists — exactly what
`souk-no-forced-protocol-deviation` rules out — so it collapsed to the
standard shape. Two corrections were then needed, and both are load-bearing.

**Starting is an explicit act, not the first iteration of a lazy generator.**
`start` returns only once the run has genuinely been handed over. Hiding the
handover inside a generator body described a pull that isn't real — a remote
agent begins working the moment it receives its input and pushes at its own
pace, whether or not souk is reading — and it meant the input went out only
if and when somebody iterated, so a cancel arriving first could strand an
agent waiting for input that was never sent.

**Cancelling is a request, so it needs its own method.** Closing souk's own
iterator is souk deciding the run is over; that is not souk's to decide. See
below.

Because the Protocol is structural there is nothing to subclass, and a local
AG-UI agent — whose own `run_stream(run_input)` is already an async generator
— is wrapped in three lines. souk ships no `InProcessProvider` class because
there is nothing for it to adapt.

That leaves one real implementation: **`GrpcProvider`**, which presents the
relay's multiplexed `AgentSession` stream as one iterator per run. The
connection is held by the provider, not per run, which is why "one `start`
per run" says nothing about how many connections a transport opens.

This port is what removed souk's one genuine transport leak. `broker.py` was
already transport-agnostic, and the run handlers are pure domain logic —
persist an event, decide a status, reduce a reply — but three of them built
`souk_pb2.AgentEventEnvelope` protobuf messages directly. They now live in
`souk/handlers.py`, in core, and `tests/test_core_is_network_free.py` asserts
no core module imports grpc, fastapi, uvicorn, starlette or httpx. That test
was verified to fail when a violation is introduced, rather than assumed.

### Cancelling: a request, with the outcome decided later

souk can ask a provider to stop. It cannot make it stop. Everything here
follows from taking that seriously.

Cancellation is only ever one of two situations, and which one is unambiguous
because Claim and RequestCancel are processed in order on the run's own
pipeline task:

1. **No provider has the run.** Nobody is working on it, souk is the only
   party involved, and it records `cancelled` outright.
2. **A provider has it.** souk asks, records `cancelling`, and waits. Nothing
   is torn down: whatever the agent emits between the request and its stream
   ending is real output, persisted and relayed like any other.

The outcome is decided only when the stream actually ends, because until then
souk does not honestly know it. **AG-UI provides no cancellation signal** —
checked against `ag-ui-protocol`: its terminal events are `RUN_FINISHED`
(outcome `success` or `interrupt`) and `RUN_ERROR`, with no cancelled event
and no cancelled outcome. Inventing one would be forcing a protocol
deviation. So the only available evidence is the *absence* of `RUN_FINISHED`,
and what separates "stopped because we asked" from "stopped because it broke"
is whether souk asked:

| how the stream ended | cancel asked? | outcome |
|---|---|---|
| `RUN_FINISHED`, interrupt outcome | either | `input-required` |
| `RUN_FINISHED` | either | `completed` — it finished anyway |
| no `RUN_FINISHED` | yes | `cancelled` |
| no `RUN_FINISHED` | no | `failed` |

Row two is the point: a provider that ignores the request and completes has
completed, and recording `cancelled` would be souk asserting something it
never verified — contradicted by the run's own output. `Run.cancelled` is
named `cancel_requested`, because that is the fact souk actually holds.

`cancelling` counts as active and as still-running for the stall sweep, so a
provider that never responds is eventually reaped rather than hanging
forever.

Getting this wrong produced a family of bugs worth recording: souk used to
enforce cancellation by cancelling its own pump task and synthesising a
stream ending. That needed a started-Event handshake, straggler absorption in
the gRPC provider, and still deadlocked — cancelling a task before its first
scheduling turn means its `finally` never runs, so the run never terminated.
All of it disappeared once souk stopped deciding on the provider's behalf.

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

### In-process is not trusted

The first version of `attach_provider` put an object in a dictionary. That
was a side door past two things souk otherwise insists on, and both had real
consequences:

- **Anything holding the Souk could claim any agent_id**, with no proof.
  Sharing a process is not a reason to be trusted; a provider's identity is
  its Ed25519 keypair whether or not there is a wire in between.
- **souk had no idea it was there.** The dictionary was invisible to the
  liveness model the roster and the offline fast-fail actually read, so an
  attached provider reported `online: False` and calls to it failed with
  "agent is currently offline" while it sat right there. Observed, not
  hypothetical.

Both now go through the same door as a remote provider:

| | remote | in-process |
|---|---|---|
| proves identity | signed registration | the same signed registration |
| says "still here" | polls for work | souk's heartbeat, same `last_seen_at` |
| souk learns it left | stops polling, ages out | `detach_provider`, marked offline at once |

`Souk.register_agents` verifies the signature and timestamp freshness — that
check used to live in the HTTP router even though registering is a domain
act, identical regardless of transport. `attach_provider` refuses an agent_id
souk never issued, so registering is a prerequisite in-process exactly as it
is remotely. The heartbeat cuts both ways: an attached provider genuinely
reads as online, and if the process wedges the heartbeat stops and it
correctly stops looking available.

### Delegation chains: core carries them, it does not vouch for them

souk cannot know how the first agent came to trust a user — SSO, an internal
login, something it has no view of — and does not try to. What it guarantees
is narrower and checkable: a claim, once made, survives every hop, and nobody
can rewrite it in transit.

That needs both halves in core. Verifying was already here; *extending* was
only in `souk-agent-sdk`, so an agent running inside souk could receive a
chain and had no way to pass it on. Provenance died at the first in-process
hop: the callee would learn who was calling now, but not on whose behalf,
with nothing tying the two together. `new_actor_chain` and
`extend_actor_chain` are now core alongside `verify_actor_chain`.

The SDK keeps its own copy rather than importing souk — a remote provider
should not have to install the gateway — so the hop format is an interop
surface, and a test builds a chain in the SDK's shape and extends it with
core's to check a remote and an in-process actor can appear in one chain.

What is rejected: a forged hop (claiming a key you don't hold), a spliced
chain (hops grafted from elsewhere), and a subject swapped partway. What is
accepted: a stale *inner* hop — earlier hops are provenance, not standing
authorization, so a run paused on a human for longer than a hop's TTL stays
resumable. Only the last hop's expiry is enforced.

### Running and querying

```python
handle = await souk.start_run(agent_id, run_input, thread_id=None)
handle.run_id
handle.thread_id
handle.is_live            # False: nothing live to consume, see below
async for event in handle.events():
    ...
handle.cancel()
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

`is_live` marks a call that resolved to a run with nothing live to consume —
already paused, already finished, or fast-failed because the target was
offline. The answer has to be reconstructed from persisted state instead, and
the two A2A methods reconstruct it *differently* (`tasks/send` replays stored
events; `tasks/sendSubscribe` emits one status update and closes), so the
condition is exposed rather than papered over.

State queries, which is what makes souk usable as an embedded component
rather than only as a relay:

```python
await souk.list_agents()
await souk.get_agent(agent_id)

await souk.get_thread(thread_id)
await souk.get_thread_messages(thread_id)
await souk.get_thread_tree(thread_id)        # multi-hop delegation lineage

await souk.get_run(run_id)
await souk.get_run_events(run_id)
await souk.resume_run(run_id, input)         # HITL: same run_id across rounds
souk.cancel_run(run_id)
souk.active_runs()                           # live in-memory dispatch state
```

### Background work belongs to the Souk

`souk.spawn(coro)` starts a task this instance owns; `await souk.aclose()`
cancels them, waits for them to unwind, and releases the pool. Two reasons
this is not `asyncio.create_task` at the call site: the loop keeps only a
weak reference to a running task, so one nothing else holds can be collected
mid-flight (which silently killed run pipelines once already), and a
fire-and-forget task has no owner at shutdown, so in-flight runs were simply
abandoned for the next process start to clean up as orphans.

Deliberately a supervised set rather than an `asyncio.TaskGroup`: a TaskGroup
cancels every sibling when one task fails, and runs must be isolated — one
agent blowing up cannot take down every other run in flight.

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
class RemoteProvider:                        # forwards to the node holding the stream
    async def start(self, run_input): ...    # same port, core unchanged
    async def cancel(self, run_id): ...
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

Pure translation, and **every rung is published**, not just the outermost
one. The shape is borrowed from `pydantic-ai`'s `UIAdapter`, which exposes
`build_run_input` / `run_stream` / `encode_stream` / `streaming_response`
separately so a caller enters wherever it needs and a server is assembled
from the same pieces rather than duplicating them:

```python
a2a = A2AAdapter(souk, public_base_url="https://souk.example.com")

# wire rung — JSON-RPC in, JSON-RPC out
await a2a.handle_rpc(agent_id, payload)

# semantic rung — real arguments, no envelope
await a2a.send_task(agent_id, message,
                    context_id=..., reference_task_ids=[...], actor_chain=[...])
await a2a.send_task_streaming(agent_id, message)
await a2a.get_task(agent_id, task_id)
await a2a.cancel_task(agent_id, task_id)

# encoding rung — SSE payloads, no response object
async for data in stream.encode(): ...
```

```python
agui = AGUIAdapter(souk)
result = await agui.run(agent_id, run_input)   # EventStream | ThreadSnapshot
async for data in result.encode(): ...
```

Publishing only the middle rung made in-process delegation absurd: one agent
calling another inside the same process had to construct
`{"jsonrpc": "2.0", "method": "tasks/send", ...}` to talk to itself, because
the envelope was the only way in. The envelope exists for transmission.
`handle_rpc` is now a thin wrapper over the semantic methods — not a second
implementation, which a test pins directly, since otherwise a remote caller
and an in-process one would slowly drift apart.

Encoding sits on the result objects (`EventStream.encode`,
`A2AStream.encode`) because getting the wire format right is part of speaking
AG-UI or A2A, not part of speaking HTTP. Left in a route, anyone serving souk
from another framework would have to reimplement it. Framing those strings
into an actual response stays with whoever owns the server.

Each protocol's two genuine outcomes are types rather than control flow
inside a route handler: AG-UI returns `EventStream` or `ThreadSnapshot` (a
thread with an active run starts nothing), A2A returns a JSON-RPC mapping or
an `A2AStream`.

Note what is *not* in any of these signatures: no `Request`, no `Response`,
no `EventSourceResponse`. An adapter never touches a framework type — the
line that keeps protocol translation out of the serving layer, and one
`tests/test_core_is_network_free.py` walks subpackages to enforce.

Keeping this in core is what preserves the hard-won mapping decisions that
would otherwise be re-derived, differently and often wrongly, by every
integrator:

- A2A `Task.id` == souk `run_id`, stable across pause/resume rounds
- A2A `contextId` == souk `thread_id`
- `referenceTaskIds` records lineage only; it never groups sessions
- an unrecognized AG-UI `threadId` mints a real thread rather than 404ing

See `souk-no-forced-protocol-deviation`: these exist so a standard AG-UI or
A2A client never has to deviate from its own spec to talk to souk.

`tasks/cancel` reports the run's real state rather than hardcoding
`cancelled`, for the same reason the database does — see the cancellation
section above.

## What `souk-server` is

The reference gateway, assembled from the above and owning every I/O
decision: reads `SOUK_*` env vars into `Settings`, applies CORS, binds the
HTTP port, binds the gRPC port, terminates TLS, runs the relay. Behavior
identical to today's `souk-server` console script.

It is also where the gRPC relay lives — outbound-only NAT traversal is
souk's headline capability, but architecturally it is a transport, so it
ships as an `AgentProvider` implementation in the server subproject rather
than in core.

## Migration status

Steps 1–4 are done; each landed with the suite green on SQLite and Postgres.

1. ✅ **Settings injection.** `CoreSettings` / `ServingSettings` passed to a
   `Souk`; engine, sessionmaker, broker and KYOK bridge become instance
   state. No import-time globals remain.
2. ✅ **Provider port.** `AgentProvider` replaces the protobuf queue on `Run`;
   the five run handlers move to `souk/handlers.py` in core. Enforced by
   `tests/test_core_is_network_free.py`.
3. ✅ **`Souk` facade.** Registration, attach/detach, `start_run` /
   `resume_run` / `cancel_run` returning a `RunHandle`, and the state queries.
4. ✅ **Protocols.** AG-UI and A2A translation extracted into
   `souk/protocols/`, with every rung published. The routers dropped from
   ~830 lines to ~300 of pure serving.
5. ⬜ **Split `souk-server`.** Move the HTTP surface, the gRPC
   servicer/provider, the KYOK endpoints and the process bootstrap into a
   sibling subproject, leaving `souk` network-free. The dependency split is
   already clean: core needs only sqlalchemy, pydantic, pydantic-settings,
   ag-ui-protocol, cryptography and pyjwt; fastapi, uvicorn, sse-starlette
   and grpcio appear in serving modules only.

Work done along the way that wasn't in the original plan, because it turned
out to be wrong rather than merely unfinished:

- The completion `ack` round trip was removed. Its only effect was a log line
  the agent could not act on, at the cost of a round trip per run and a
  worst-case 5s stall.
- Cancellation was re-modelled as a request whose outcome is decided when the
  stream ends (see above), adding the `cancelling` status.
- In-process providers were made to register and prove identity like remote
  ones, fixing an attached-but-reported-offline bug.
- Actor-chain construction moved into core so provenance survives an
  in-process hop.

## What breaks

Everything below is intentional; souk is unreleased at 0.1.0, which is the
cheapest possible moment to make these changes.

- `from souk.server import app` disappears. No compatibility shim.
- `souk.config.settings` as an import-time global disappears.
- The `souk-server` console script moves to the new subproject.
- `proto/souk.proto` field 5 (`ack`) is reserved rather than reused, since an
  old SDK's frame must never be misread as something else. The SDK stops
  waiting for it.
- Otherwise the wire is unchanged, so `souk-agent-sdk`, `souk-client-sdk` and
  `souk-directory` keep working against a running gateway. `souk-agent-sdk`
  needed only the ack removal.
- `thread_history.status` gains `cancelling`, via a migration that is
  dialect-branched (Postgres re-adds the CHECK in place; SQLite cannot alter
  a constraint, so batch mode rebuilds the table).

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

Partly done already: `KyokBridge` and `PendingCompletion` moved from
`api_llm_bridge` into `souk/kyok.py` when live dispatch state stopped being a
module singleton, since the bridge is structurally a second broker and is
held per-Souk for the same reasons. What remains for step 5 is the three
`/kyok/*` endpoints and the `LLMBridge` port.
