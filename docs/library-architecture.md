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
  providers/          #   AgentProvider port + InProcessProvider
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
souk.attach_provider("my-agent", InProcessProvider(my_agent_fn))
```

An agent reaches souk through an `AgentProvider`. Nothing about that port is
network-shaped:

```python
class AgentProvider(Protocol):
    """souk's outbound half: what souk needs to say to a running agent."""
    async def deliver_input(self, inbox: RunInbox, run_input: RunAgentInput) -> None: ...
    async def signal_cancel(self, run_id: str) -> None: ...
    async def ack(self, run_id: str) -> None: ...


class RunInbox(Protocol):
    """The return path souk hands the provider for one specific run."""
    run_id: str
    # Raw payload by design, not an oversight — this is the relay path;
    # see "Typed data, and where typing stops" below.
    def relay_event(self, event: dict) -> None: ...
    def finish(self) -> None: ...
```

Both halves are needed, and they are deliberately asymmetric. Outbound is
method calls; inbound is `RunInbox`, which enqueues commands onto that run's
`in_queue` rather than mutating anything directly. That asymmetry is not
stylistic — `broker.py` guarantees exactly one pipeline task per run is ever
applying changes to it, so every inbound signal has to serialize through the
queue. (Its module docstring records what the alternative cost: an earlier
version let four modules poke a shared dataclass, and cancellation took two
rounds of ordering fixes before it worked.) `RunInbox` exists so a provider
never has to reach into `broker.get(run_id).in_queue` to do that itself.

### What each call means

The full life of one run, and where each port method lands:

```
agent → PollForWork              "any work for me?"  → souk answers with run_id
agent → AgentSession, empty frame "I'm taking it"     → becomes a Claim command
souk  → deliver_input             ★ hands over the run's RunAgentInput
agent → events…                   → inbox.relay_event(...) per event
agent → end_of_stream             → inbox.finish()
souk  → ack                       "every event persisted and relayed"
```

`deliver_input` is the one that is easy to misread as bookkeeping: it is the
step that actually gives the agent its work. Claiming a run only tells the
agent *which* run it got; without `deliver_input` it knows the id and nothing
else — not the thread, not the messages, not the tools. What it carries is
AG-UI's `RunAgentInput` (threadId, runId, messages, tools, state, resume).

`signal_cancel` asks a claimed run to stop producing events. `ack` confirms
souk has durably persisted *and* relayed everything for that run, so an SDK
knows the call was fully consumed rather than merely sent. `ack` is a no-op
for an in-process provider — there is no delivery to confirm when there is no
wire.

This port is what removes the one genuine transport leak in today's code.
`souk/broker.py` is already transport-agnostic (plain asyncio, commands are
pure data), and four of the five handlers in `grpc_server.py` are pure domain
logic. The leak is that `_handle_claim`, `_handle_finish` and `_handle_cancel`
construct `souk_pb2.AgentEventEnvelope` protobuf messages directly onto
`Run.agent_outbound`. Replacing that queue with an `AgentProvider` moves all
five handlers into core unchanged in substance, and leaves protobuf
serialization to the one implementation that needs it.

Two implementations:

- **`InProcessProvider`** — wraps a local callable. No socket anywhere.
- **`GrpcProvider`** (in `souk-server`) — serializes to `AgentEventEnvelope`
  over an `AgentSession` stream. Exactly today's behavior.

Both go through the same broker machinery, so claim races, cancellation
semantics and ack ordering are implemented once, not per transport.

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

There is one deliberate exception, and it is not laziness. souk is a relay:
it forwards a provider's event stream to the caller without interpreting it,
which is what `souk-no-forced-protocol-deviation` protects. Strictly parsing
every relayed event into a model would introduce two real failure modes —
rejecting a valid event from a newer AG-UI version souk does not know yet,
and silently rewriting payloads on the reparse/reserialize round trip (field
order, nulls, defaults).

The relay path therefore parses leniently and forwards faithfully: souk
reads only the fields it actually makes decisions on (`type`, and
`outcome` for pause detection — see `pause.interrupt_outcome_of`) and relays
the original payload untouched. Parse for souk's own decisions; never
validate-and-rewrite something a provider owns.

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
