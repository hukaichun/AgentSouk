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
| ③ **Serving** | uvicorn, binding ports, TLS, whatever carries a worker's calls | ❌ never |

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
souk/souk/            # the library. Network-free.
  core.py             #   ① Souk: the domain surface an embedder uses
  broker.py           #     live dispatch: one queue pair and one task per run
  handlers.py         #     what a command does to a run (persist, decide)
  providers.py        #   the Provider port (agent_id + the AG-UI shape)
  worker.py           #   the loop that claims work and drives a provider
  repo.py schema.py   #   the database, and the only thing core knows about
  protocols/          #   ② agui, a2a, kyok — pure translation, no I/O
souk-server/souk_server/   # ③ the reference gateway, a separate distribution
  server.py           #   process bootstrap, CORS, TLS, both listeners
  api_*.py deps.py    #   FastAPI wiring of souk.protocols, and the models
  grpc_server.py      #   the worker channel (the NAT-traversal relay).
                      #   gRPC today; becoming a WebSocket, see below
```

`souk` depends on SQLAlchemy (plus a driver), pydantic-settings,
ag-ui-protocol, cryptography, pyjwt and alembic — and on no transport at all.
It cannot import `fastapi`, `uvicorn`, `sse-starlette`, `grpcio` or
`websockets`, because none of them is installed with it: that is the invariant this whole design
exists to protect, and packaging is what makes it structural rather than a
matter of discipline. A test enforces it too (see souk/tests/
test_core_is_network_free.py) — packaging protects against the accident, not
against someone adding the dependency back on purpose.

`souk-server` is a sibling subproject like `souk-agent-sdk` and
`souk-client-sdk`, not an extra of `souk`. A separate distribution makes the
boundary impossible to erode by accident: core cannot grow a `uvicorn`
import without someone noticing they're in the wrong package.

## The core object

```python
from souk import Souk, Settings

souk = Souk(Settings(database_url="sqlite+aiosqlite:///./souk.db"))
await souk.start()          # orphan cleanup + health sweeps
...
await souk.aclose()         # stop what start() started, release the pool
```

`health()` sits beside them: whether the database answers, which migration
it is at against the one this code was built for, and whether the background
half is running — reported as facts, because "the process is alive" and "it
can serve" are different questions and only the caller knows which it is
asking. souk-server maps them onto `/healthz` (answers from nothing, so a
database blip cannot restart every replica) and `/readyz` (503 when not
ready). Both are unauthenticated, so `Health` carries no connection string
and no driver message — only the exception's type name.

`start` runs once: a second call is a no-op, and that is the point rather
than a convenience. Reconciling orphans is idempotent over rows from before
the process began, *not* over a run created since — so a second pass would
mark that one failed. The serving layer used to call its own startup twice
by design (once before opening its second listener, once from the ASGI lifespan)
with a comment explaining why that was harmless; it was harmless only
because the window between them was usually empty.

Both halves are optional for an embedding caller — runs dispatch either way.
What skipping `start` costs is exactly what it does: the reconciliation, and
every health sweep after it.

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
socket: `http_host`, `http_port`, `cors_allow_origins`, the
`*_tls_cert_path` / `*_tls_key_path` values, and (until the worker channel
moves onto the HTTP app) `grpc_host` / `grpc_port`. Which of these exist is
the serving layer's business to change; none of them can reach core.

**`public_http_url` is the one genuine boundary case.** It looks like
serving (it is a URL) but is used as *content*: `api_a2a` interpolates it to
build the URLs advertised in an Agent Card. It belongs to neither side as a
setting — core should not know what it is called on a network — so it becomes
an argument the server passes when constructing the protocol adapter:

```python
A2AAdapter(souk, public_base_url="https://souk.example.com")
```

### Attaching providers: a worker claims, core never calls out

```python
await souk.attach_provider(public_key, my_provider, [translator, summarizer], max_claim=2)
```

A **provider** is one identity offering one or more agents — what souk has
always meant by the word: registration is per provider and carries a batch of
agents, the roster groups by it, and `souk-agent-sdk` is one process holding
several `AgentHandle`s. The id it is attached under is its Ed25519 public
key, because that is the only identity it has (see "A provider is its key"
below). The port is that identity, in souk's own process:

```python
class Provider(Protocol):
    def run_stream(self, agent_id: str, run_input: dict) -> AsyncIterator[AgentEvent]: ...
```

An **agent** is still exactly what AG-UI says it is — a run input in, events
out. The only addition is `agent_id`, on the method rather than smuggled into
the input, because AG-UI's RunAgentInput carries no agent identity and a
provider serving a translator and a summarizer has to know which one a run is
for. The provider routes; souk does not hold a callable per agent.

What changed is not the provider — it is who drives it. souk used to *call* a
provider; now a **worker** claims runs for it and pushes events back, and
that worker is per provider, which is why concurrency (`max_claim`) is a
budget across everything it hosts.

Declaring the identity first is also what makes attaching checkable: every
`agent_id` must be one that key actually registered. Attaching used to derive
the provider from the agent, so there was nothing to verify against — the
answer was whatever the agent row said.

Every worker runs the same loop, in-process or across a wire:

```python
while True:
    runs = await souk.claim_work(token, agent_ids, max_claim=capacity)
    for run in runs:
        spawn(execute(run))      # events reported back per run
```

against three core methods and nothing else:

| | |
|---|---|
| `claim_work(token, agent_ids, max_claim, on_cancel)` | take runs — **with their input** |
| `report_event(run_id, event, claimed_by)` | one AG-UI event for a run you hold |
| `finish_run(run_id, claimed_by)` | that run's stream has ended |

souk ships that loop (`souk/worker.py`) so an attached provider uses it
rather than a shortcut; `souk-agent-sdk` runs the same loop on the far side
of a wire, where some transport carries those three calls and one
notification back the other way (souk asking a run to stop).

**Which transport is not core's business, and core says so nowhere.** That
sentence used to read "on the far side of gRPC, where `PollForWork` carries
`claim_work` and `AgentSession` carries the other two", and the same naming
had spread through `broker.py`, `identity.py`, `repo.py` and `kyok.py` — core
describing itself in the vocabulary of one deployment's wire. It is gone: the
contract is the three methods and the cancel notification, and a transport
implements framing around them.

That matters right now, because the transport is changing. The base server
mode is **HTTP + WebSocket**, so the gRPC service — a second listening port,
a `.proto`, generated stubs and a build step to produce them — is being
removed in favour of one WebSocket on the app that already exists. Core is
untouched by that, which is the test of whether this boundary was real: the
worker loop, the ownership checks, the long-poll wait and the cancel
notification are all the same calls afterwards. The landing is `souk-server`
and `souk-agent-sdk`'s job, tracked separately from this document.

Three properties follow, and each replaces something the previous port needed
a mechanism for:

**Claiming is the hand-over.** A claimed run comes back with its
RunAgentInput, so a worker leaves the call able to start. There is no second
step in which souk delivers the input, so there is no window where a run is
claimed but its worker is waiting to be told what to do — the window the old
`start()` handshake existed to close, and which a cancel arriving first could
strand an agent in.

**Claiming is also being claimed.** `RunBroker.claim` marks the run taken and
queues its `Claim` with no `await` in between, so nothing can observe a run
that has been handed out but belongs to nobody. The old model handed out
run_ids and waited for the provider to come back and say what it took, which
needed a second cancelled-check to cover the gap.

**Reporting is authorized.** Every event names the identity that claimed the
run, and core checks it. Holding an authenticated connection is not the same
as holding a particular run; without the check, any connected provider could
push events into any run_id it could guess.

`attach_provider` is not a shortcut past any of this. It refuses an agent_id
that key never registered, mints a session token for that identity, and
claims through the same `claim_work` with the same ownership filtering — see
"In-process is not trusted" below.

The port this replaced is described in "The provider should be a worker"
further down, along with what it cost. What it did get right is kept: souk's
one genuine transport leak went with it. `broker.py` was
already transport-agnostic, and the run handlers are pure domain logic —
persist an event, decide a status, reduce a reply — but three of them built
`souk_pb2.AgentEventEnvelope` protobuf messages directly. They now live in
`souk/handlers.py`, in core, and `tests/test_core_is_network_free.py` asserts
no core module imports grpc, fastapi, uvicorn, starlette, httpx or
websockets — the last one listed before anything imports it, so the
WebSocket that replaces gRPC cannot leak in either. That test
was verified to fail when a violation is introduced, rather than assumed.

### Cancelling: a request, with the outcome decided later

souk can ask a provider to stop. It cannot make it stop. Everything here
follows from taking that seriously.

Cancellation is only ever one of two situations, and which one is unambiguous
because Claim and RequestCancel are processed in order on the run's own
pipeline task:

1. **Nobody has the run.** No worker claimed it, souk is the only party
   involved, and it records `cancelled` outright.
2. **A worker has it.** souk asks, records `cancelling`, and waits. Nothing
   is torn down: whatever the agent emits between the request and its stream
   ending is real output, persisted and relayed like any other.

Asking is a plain synchronous notification — the callback a worker supplied
when it claimed (`claim_work`'s `on_cancel`), invoked once and not awaited,
which is all a request can honestly be. It used to be
`await provider.cancel(run_id)`, an await into provider code *on the run's
own pipeline task*, so a slow or wedged provider could stall the queue its
own events arrive on.

Whether a worker complies is the worker's business. souk's in-process worker
does, by cancelling the task running the agent; so does the reference SDK,
with the cancel frame. Both are provider-side decisions, and core is written
so that a worker which ignores the request and finishes normally is recorded
as having finished — verified end to end, not only in core (see the outcome
table's second row).

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

The mirror-image mistake is staying quiet about a verdict souk *has* reached.
Row four — `failed` — was recorded and never told to anyone: a provider whose
`run_stream` raised produced an HTTP 200 whose event stream closed in 0.1s
having emitted nothing, which a client cannot tell from an agent with nothing
to say. souk now emits a terminal `RUN_ERROR` in exactly that case, persisted
as well as relayed so a reconnecting caller reads the same account. It is not
a deviation and not a decision on anyone's behalf: `RUN_ERROR` is AG-UI's own
terminal event, the verdict is already souk's and already in the database,
and an agent that reported its own failure is left alone (`Run.saw_run_error`
is what prevents saying it twice). `cancelled` still gets nothing — there is
no cancelled event to send, and the only party who would read it is the one
who asked.

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

### A provider is its key, and has no other id

There used to be a second one. Registration carried an `sdk_client_id` — a
string the client picked for itself, defaulted by the SDK to
`sdk_{random hex}` — and it was in the signed payload, in an `agents` column,
in the session token, and, decisively, in the query that decides what a token
may claim. The public key was right there in the same table the whole time.

Two things were measured before removing it, and neither is subtle once the
question is asked out loud — *is this an identity?*

- **Two unrelated keypairs picking the same string were both accepted**, and
  the second one's session token claimed the first one's run, received its
  input, and could report events into it. Nothing about that string was ever
  verified; the signature only proved the key held by whoever signed it.
- **Two processes of one real identity could not share their own work.** The
  SDK mints a fresh string per process, and registration overwrites the
  column, so after the second process registered, the first could no longer
  claim its own agent's runs — valid token, live connection, its own key.

So it was neither an identity nor a usable per-process label, and every job
it held is done better by the key: `UNIQUE(public_key, name)` is what
ownership already meant, de-listing already swept by public_key, and the
providers table was already keyed by it. Claiming, reporting and the session
token all key off `public_key` now, and the column is gone (see the
migration).

What genuinely needs "which connection" — delivering a cancel request to a
live stream — needs no id in the protocol at all: the servicer holds the
connections and only has to know which belong to this provider, which is the
key again. Two processes of one provider both get asked; the one without the
run ignores it. Which is the same answer souk gives everywhere else: it
carries data to a provider and checks it arrived, and how that provider
arranges itself is its own business.

### In-process is not trusted

The first version of attaching an agent put an object in a dictionary. That
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

Both now go through the same door as a remote provider — and since the
worker model, it is not merely the same *kind* of door but literally the same
call:

| | remote | in-process |
|---|---|---|
| proves identity | signed registration | the same signed registration |
| takes work | `claim_work`, with a session token | `claim_work`, with a session token |
| says "still here" | claiming marks it seen | claiming marks it seen |
| souk learns it left | stops claiming, ages out | `detach_provider`, marked offline at once |

`Souk.register_agents` verifies the signature and timestamp freshness — that
check used to live in the HTTP router even though registering is a domain
act, identical regardless of transport. `attach_provider` refuses an agent_id
this provider never registered, so registering is a prerequisite in-process
exactly as it is remotely, and the token it mints is scoped to that identity
— an attached provider cannot claim another's work any more than a remote one
can.

Row three used to be the exception: an attached provider was kept alive by a
souk-side heartbeat, because it never asked for anything and so produced no
evidence it was there. A worker claims, and claiming is that evidence, so the
heartbeat is gone — a second mechanism for a fact the claim loop already
produces. It still cuts both ways: an attached agent reads as genuinely
online, and if the process wedges its worker stops claiming and it correctly
stops looking available.

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

- **Streaming** — AG-UI, and A2A's `SendStreamingMessage`, consume events as
  they arrive.
- **Collect-and-return** — A2A's `SendMessage` drains the whole run and
  answers with one `Task` object. Not streaming, but still needs every event.
- **Address it later by id** — A2A's `tasks/get` and `tasks/cancel` come back
  to a run long after the call that started it. `Task.id` *is* the `run_id`,
  so the id must be available without consuming (or even starting) a stream.

`is_live` marks a call that resolved to a run with nothing live to consume —
already paused, already finished, or fast-failed because the target was
offline. The answer has to be reconstructed from persisted state instead, and
the two A2A methods reconstruct it *differently* (`SendMessage` replays
stored events; `SendStreamingMessage` emits one status update and closes), so the
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

## The provider should be a worker, not something core calls

Built. The port it replaced had core *call* a provider and pull its events;
it is inverted, so a provider is a task that claims runs and pushes back.

**What the pull model cost.** A single event crossed three queues and two
routing tables:

```
agent event ─wire→ handle_incoming
                     → GrpcProvider._runs[run_id]   ← routing table 1 (transport)
                     → _pump iterates the generator
                     → broker._runs[run_id].in_queue ← routing table 2 (core)
                     → pipeline persists
                     → out_queue → SSE
```

Before the provider port existed it was two queues and one table:
`handle_incoming` looked the run up in the broker and pushed straight into
its `in_queue`. The second table and the `_pump` task existed purely to turn
a push (events arriving on a wire) into a pull (core iterating a generator).

So an earlier version of this document was wrong to call that table a
property of the wire and conclude it belongs in the transport. It is a
property of the *pull model*. Core already has the only routing table
needed — the broker's run registry. It is two and one again:

```
agent event ─wire→ handle_incoming
                     → souk.report_event(run_id, event, claimed_by=…)
                     → broker._runs[run_id].in_queue  ← the only table
                     → pipeline persists
                     → out_queue → SSE
```

`tests/test_event_path.py` holds that shape: it checks the event reaches the
run's queue as the same object, and that no run_id appears anywhere in the
gRPC servicer's state while three runs are in flight over one connection.

**What it also cost: backpressure.** A remote provider could say
`max_claim=2`. An in-process one had no equivalent: attaching a provider and
starting five runs started all five at once, measured. That asymmetry was the
same root cause — the remote side pulled to claim, then core pushed the input
back to it, and the in-process side only had the push half. `attach_provider`
now takes `max_claim`, and both halves are one loop against one method, so
there is no second implementation to keep in step.

**The shape.** A provider is a worker loop, identical in-process and remote:

```python
while True:
    runs = await souk.claim_work(token, agent_ids, max_claim=capacity)
    for run in runs:
        spawn(execute(run))      # events pushed back per run
```

The agent stays exactly what AG-UI says it is (`run_stream(input) -> events`).
What changes is that a *provider* stops being conflated with an *agent*: the
provider is the worker hosting one, which is why it is the thing that should
own concurrency and claiming.

Consequences, as predicted: `_pump`, the `Claim` command's provider payload
and `GrpcProvider._runs` are gone; `claim_work` hands back runs with their
input; core gained `report_event` / `finish_run`. Three more followed that
were not predicted, and each is worth recording:

- **A claim frame is no longer needed on the wire**, because the input goes
  out with the claim. The AgentSession exchange lost two of its four steps,
  and `PendingRun` gained a `json_payload`. `assign_provider` went with it.
- **Reporting needed authorization.** Turning the return path into a public
  core method made the question unavoidable: previously an event could only
  arrive by souk iterating a stream it had itself asked for. Every event and
  every `finish_run` now names the claiming identity and is checked against
  `Run.claimed_by`. The old model's equivalent — any AgentSession could send
  a claim frame for any run_id — was a real hole, and it closed here.
- **A dropped connection stopped being an outcome.** Under the pull model
  souk was reading *from* that connection, so losing it ended the runs it
  carried: `close_all` synthesised a stream-ending for each. Pushed events
  are addressed by run_id, so a worker can reconnect and report the rest,
  including how the run ended. souk records nothing when a connection dies;
  a worker that really is gone is caught by the stall sweep. Probed against
  a real server going away mid-run: the SDK reconnected and the run reached
  its true outcome 1.5s later, well inside the 120s sweep.

**It also makes an acknowledgement worth having.** souk removed the old
completion `ack` because it arrived after the agent had already produced and
discarded its events, so the only possible response was a log line. In a push
model the worker still holds what it sent, so a confirmation is something it
can act on — retry, or don't advance its cursor. At-least-once delivery
becomes expressible, where under the pull model there is no moment at which
the worker could ask. Still not built: the SDK holds unwritten frames across
a reconnect and flushes them before closing a stream, but a frame whose write
fails mid-flight is dropped, because there is nothing to retry against.

**Two things this got wrong first, both found by running something.**

An in-process worker that slept between claims (mirroring the SDK's
`poll_interval`) added up to that interval of latency to every run started
while it was sleeping — the whole test suite got 3× slower, which is how it
surfaced. The fix is that a worker never sleeps to wait: it blocks *in* the
claim call, which already returns the moment work is enqueued. What differs
is only how long it is willing to block — a full long poll when idle, a short
interval when busy, because what it is waiting for then is its own capacity,
which souk cannot observe.

And the first probe of a dropped connection didn't drop one: cancelling the
gRPC call locally raises `CancelledError`, which the SDK correctly reads as
"we are shutting down", so it exited instead of reconnecting. Taking the
server away produces `UNAVAILABLE`, which is the case worth testing. Reading
the code would not have distinguished those two.

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

The obstacle to multiple replicas used to be stated as **connection
affinity**: a provider's `AgentSession` is pinned to one process, so if node A
holds agent X's stream and a run for X is created on node B, B cannot
dispatch it.

Inverting the provider shrank that. A worker is not dispatched *to*; it comes
and claims, over a plain call that any node can answer, and it reports back
by run_id rather than down the connection the run arrived on. Nothing is
bound to the connection a run arrived on any more.

**What two replicas actually do today**, measured against one database rather
than argued about — every line here is something a load balancer will do to
you by accident:

| | |
|---|---|
| the roster | shared, both nodes agree |
| a run created on A | invisible to a provider claiming on B; only A can hand it out |
| that run's events reported to B | `report_event` returns False, nothing persisted, and the worker never learns — it pushed and moved on |
| the SSE consumer | reads `out_queue` on whichever node owns the run |
| a third replica booting | `fail_orphaned_runs` is DB-wide, so it marks A's *live* runs `failed` while A keeps relaying them |

The last row is the one that bites first: it needs two nodes, not many, and
it is silent. The stall and unclaimed sweeps have the same shape — every
replica reaps every run.

So the failure mode is not slowness, it is two nodes disagreeing. Three
things would be required, in this order:

1. **Ownership on the sweeps.** Either one elected sweeper, or a lease on the
   run so a node reaps only what it owns and only what has genuinely expired.
   Small, no API change, and the only item here that is already doing damage.
   Note where it belongs when it happens: "which runs am I responsible for"
   is the broker's question — in memory the answer is its registry, and
   distributed it is the runs recorded against this instance — so it is an
   operation on whatever the broker becomes, not a rule in `health.py`.
2. **A shared claim queue**, so `enqueue_run` on A is claimable on B — an
   INSERT plus a notify, and a `SELECT … FOR UPDATE SKIP LOCKED` where
   `RunBroker.claim` is now. Runs are already rows; this is mostly a query.
3. **Cross-node relay for a run's output**, so an SSE consumer on A receives
   what a worker reported to B. This one is a decision before it is code:
   notify-and-tail `run_events` (no new infrastructure, but it puts the
   database on the read path `broker.py` deliberately keeps it off), a
   pub/sub bus (keeps the hot path clear, adds a dependency), or routing the
   consumer to the node that owns the run (nothing new to install, but souk
   stops being stateless behind a plain load balancer). The same mechanism
   carries souk's cancel request to whichever node holds that worker's
   stream, which is the last of the affinity problem.

**What is already in place, stated exactly**, because an earlier version of
this document oversold it and the overstatement was believed:

- The broker is an instance owned by a `Souk` and accepted in its
  constructor, not a module-level singleton. That part is real.
- Its wake mechanism is a plain `asyncio.Event`, not anything transport-
  shaped, so a `LISTEN/NOTIFY` implementation could sit behind it.
- `Run` is now the broker's own. Nothing outside `broker.py` holds one: a
  caller gets a `RunSnapshot` (a copy of the facts, no live references) and
  affects a run through operations — `push`, `subscribe`, `request_cancel`.
  The one exception is deliberate: `handlers.py` receives
  the live object, because the handlers *are* the pipeline's inner loop and
  the broker is what dispatches them. A different broker implementation
  brings its own.
- There is still **no declared interface** — no Protocol, no ABC. The
  surface is now small enough to be one, but writing it from the in-memory
  implementation alone is how the last "swap-in seam" came to be believed:
  it is only an interface once a second implementation has met it.

Encapsulating it made the shape visible, and the shape is better than it
looked: a `Run` mixes three separable things. Its identity and input are
immutable facts. Its round state (`seq`, `saw_run_finished`,
`pause_payload`, `round_starting_seq`) belongs to whichever node runs that
run's pipeline and never needs to travel. Only the two queues and the
registry are genuinely distributed state — so a distributed broker has to
move less than the old all-in-one object suggested.

It also surfaced a bug that had been hiding behind the leak. Handing out the
live `Run` meant a caller's reference kept that run's queue alive, so a
handle taken now and read later still replayed everything. Subscribing by
id instead is lazy, and a short run that finished — and was forgotten —
before anyone read it returned nothing at all. Handles, the AG-UI relay and
A2A's streaming branch now all subscribe at the moment they are created
rather than at first iteration. The in-process test that starts five runs
and reads them after they finish is what caught it.

**Provider-side scaling is deliberately not souk's problem.** A provider
running several processes shares one keypair, and souk cannot tell those
processes apart: any of them may claim that provider's work, and any of them
may report events for a run another one claimed. That is not a gap to close —
souk's contract is with the *identity*, and how a provider divides work
between its own processes is its own business, exactly like how it divides
work between threads. The consequence to know is that souk will not move a
run between instances: if the instance holding a run dies, nobody finishes
it, and the run ends as `failed` when the stall sweep notices
(`run_stall_timeout_seconds`, 120s by default).

Building 2 and 3 now would be premature. Leaving the door shut would not.

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
`{"jsonrpc": "2.0", "method": "SendMessage", ...}` to talk to itself, because
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

### A2A comes from a library now, because hand-writing it failed twice

AG-UI always arrived as a dependency: souk requires `ag-ui-protocol`, so
`RunAgentInput` and the event types come from the spec's own package and an
upgrade is a version bump. A2A was hand-written — method names as string
literals, wire shapes as dict literals. Nothing tells you the spec moved.

It had moved twice.

| | souk shipped | v0.3 | v1.0 (current) |
|---|---|---|---|
| send | `tasks/send` | `message/send` | `SendMessage` |
| stream | `tasks/sendSubscribe` | `message/stream` | `SendStreamingMessage` |
| text part | `{"type": "text", ...}` | `{"kind": "text", ...}` | `{"text": ...}` — `Part` is a oneof, the field name *is* the type |
| stream item | bare update, `{"id": ...}` | bare update, `{"kind": ..., "taskId": ...}` | wrapped in a `StreamResponse` (`statusUpdate` / `artifactUpdate`) |
| terminal | `final: true` | `final: true` | no such field — the stream ending is the signal |
| state | `"completed"` | `"completed"` | `"TASK_STATE_COMPLETED"` |
| role | `"user"` | `"user"` | `"ROLE_USER"` |
| card path | `/.well-known/agent.json` | same | `/.well-known/agent-card.json` |
| card url | one `url` + `preferredTransport` | same | `supportedInterfaces[]`, each with its own binding and version |

A client built from the published schema got `-32601` on its first call. That
was found by pointing a real client at a running souk — and the *first* fix
was wrong too, targeting v0.3, because its shapes were read out of a module
named `a2a.compat.v0_3` without asking what it was compatibility *for*. Its
README's opening line says: for v1.0 systems to interoperate with legacy v0.3
ones.

So souk now depends on `a2a-sdk` and holds no A2A vocabulary of its own:

- every emitted shape is built from `a2a.types.a2a_pb2` and serialised with
  protobuf's JSON mapping, so field names and enum spellings come from the
  descriptor
- the JSON-RPC method names are read off the `A2AService` descriptor —
  `_method("SendMessage")` raises at import if the service stops offering it
- `PROTOCOL_VERSION` is the SDK's `PROTOCOL_VERSION_CURRENT`, and the
  well-known card path is its `AGENT_CARD_WELL_KNOWN_PATH`

souk emits v1.0 and only v1.0, and accepts every spelling it has ever
offered — v1.0, v0.3, and its own original. That asymmetry is deliberate:
lenient inbound costs an `or`, strict inbound breaks callers for nothing. The
SDK ships the same accommodation (`enable_v0_3_compat`), so it is the spec's
own idea of politeness rather than souk's invention.

The card's *path* is the one place that accommodation was not made, after
being tried. Serving the pre-v1 URL looked like the same courtesy, but it
answered that URL with the v1.0 body — no top-level `url`, no
`preferredTransport` — so a client old enough to want the old path found a
card it could not use to locate the RPC endpoint. Half an accommodation is
not one. Answering it with a v0.3-shaped body would be a real one, and that
is a gateway decision: it is a URL, and URLs are not this library's to
choose.

Also gained, since the spec had them and souk didn't: `Message.taskId`
resolves to that task's thread (an unknown one is `-32001`, not a quietly
fresh conversation), and `SubscribeToTask` for rejoining a stream.

**The cost is real and was paid knowingly.** `a2a-sdk`'s base install brings
`httpx`, `requests`, `protobuf` and `google-api-core` into core — the sort of
weight the packaging split exists to keep out. Two consequences:

- `tests/test_core_is_network_free.py` is now the *only* thing stopping a
  core module importing `httpx`; packaging no longer backs it up. Its
  docstring says so.
- `protobuf<7` is now pinned transitively, which downgraded `grpcio-tools`
  and made previously-generated `souk.proto` stubs unloadable
  (gencode 7.35.1 against runtime 6.33.6). They are gitignored and
  regenerated by `scripts/gen_proto.sh`, so this bites a stale working tree
  rather than CI — but a protobuf major in either direction will bite again.

None of the SDK's client, server or transport code is imported; those live
behind extras (`http-server`, `grpc`, `fastapi`) that are deliberately not
requested. `a2a.types.a2a_pb2` imports nothing from the forbidden list, which
is what makes this survivable at all.

**Where this rule is not yet applied:** A2A also defines a gRPC binding, and
souk serves A2A over JSON-RPC only. If that changes, the stubs must come from
`a2a-sdk`, not from a hand-written `.proto`. souk's own `proto/souk.proto` is
a different hop entirely — provider-to-souk work claiming, which A2A has no
concept of — and its envelopes carry opaque `json_payload`, so it duplicates
no A2A type.

## What `souk-server` is

The reference gateway, assembled from the above and owning every I/O
decision: reads `SOUK_*` env vars into `CoreSettings` + its own
`ServingSettings`, applies CORS, binds its listeners, terminates TLS, runs
the relay. Behaviour is what the `souk-server` console script always did;
what changed is which distribution it ships in.

It is also where the worker channel lives — outbound-only NAT traversal is
souk's headline capability, but architecturally it is a transport, so it
ships in the server subproject rather than in core. It carries a worker's
three core calls, plus souk's cancel request back the other way, and holds
nothing of its own beyond the open connections it can reach one on.

**The base server mode is HTTP + WebSocket**, so that channel is moving off
gRPC: one listener instead of two, and no `.proto`, generated stubs or
codegen step in the build. This section still describes the gRPC shape
because that is what is in the tree; the swap is tracked as its own piece of
work. What it must not touch is anything above this heading — if it does,
the boundary this document is about was not real.

## Migration status

All six are done; each landed with the suite green on SQLite and Postgres.

1. ✅ **Settings injection.** `CoreSettings` / `ServingSettings` passed to a
   `Souk`; engine, sessionmaker, broker and KYOK bridge become instance
   state. No import-time globals remain.
2. ✅ **Provider port.** The protobuf queue on `Run` goes behind a port, and
   the five run handlers move to `souk/handlers.py` in core. Enforced by
   `tests/test_core_is_network_free.py`. The port itself was later replaced
   by step 5 — what survived is the handlers' move and the invariant.
3. ✅ **`Souk` facade.** Registration, attach/detach, `start_run` /
   `resume_run` / `cancel_run` returning a `RunHandle`, and the state queries.
4. ✅ **Protocols.** AG-UI and A2A translation extracted into
   `souk/protocols/`, with every rung published. The routers dropped from
   ~830 lines to ~300 of pure serving.
5. ✅ **Invert the provider into a worker.** `souk/worker.py` and the
   `claim_work` / `report_event` / `finish_run` trio replace the
   `AgentProvider` port; the event path is back to two queues and one table,
   in-process work is throttled by `max_claim` like remote work, and both run
   the same loop. See the section below for what it cost to keep and what it
   changed on the wire.
6. ✅ **Split `souk-server`.** The HTTP surface, the gRPC servicer, the KYOK
   endpoints, the request/response models, `ServingSettings` and the process
   bootstrap are a sibling distribution; `souk` declares no transport at all.
   That turns the invariant from a rule into a fact: core cannot import
   fastapi or grpcio because they are not installed alongside it, verified by
   importing `souk.core` in an environment where every one of them is absent.
   `tests/test_core_is_network_free.py` stays — packaging protects against
   the accident, not against someone adding the dependency back — and it lost
   its allow-list, because there is no longer a module under `souk/` that is
   exempt.

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
- Reported events became authorized: a worker may only speak for runs it
  actually claimed. Under the pull model there was nothing to authorize —
  core only ever read streams it had asked for — so inverting the direction
  is what turned this into a question, and an unchecked claim frame into a
  hole worth closing.

## What breaks

Everything below is intentional; souk is unreleased at 0.1.0, which is the
cheapest possible moment to make these changes.

- `from souk.server import app` disappears. No compatibility shim.
- `souk.config.settings` as an import-time global disappears.
- The `souk-server` console script moves to the new subproject.
- `proto/souk.proto` field 5 (`ack`) is reserved rather than reused, since an
  old SDK's frame must never be misread as something else. The SDK stops
  waiting for it.
- The provider-facing wire changed with the worker model, so an SDK older
  than this must be updated (`souk-client-sdk` and `souk-directory` are
  caller-facing and unaffected):
  - `PendingRun` gains `json_payload`, the run's RunAgentInput. Claiming is
    the hand-over.
  - The AgentSession claim frame and souk's input frame are gone with it. A
    session now carries events and `end_of_stream` up, and `cancel` down.
  - `Souk.attach_provider(agent_id, provider)` becomes
    `attach_provider(provider_id, provider, agent_ids, max_claim=…)`: keyed
    by the provider identity that registered, not by one of its agents.
    `souk/providers.py`'s port keeps `agent_id` and loses `start`/`cancel` —
    one method, `run_stream(agent_id, run_input)`, iterated by the worker
    (`souk/worker.py`) rather than called by core. `assign_provider` is gone.
- **`sdk_client_id` is gone**, everywhere: out of `POST /agents/register`'s
  body, out of `Souk.register_agents`, out of the session token (which now
  carries the `public_key`), and out of the `agents` table. A provider's identity is its keypair — see "A
  provider is its key" above for the two things that were measured before
  removing it.
- `SoukAgentClient` is now **`SoukProvider`**, and loses its `sdk_client_id`
  parameter. The rename is not cosmetic: the class is one identity hosting
  several agents, which is what souk means by a provider, and it now
  satisfies the same `run_stream(agent_id, run_input)` port as an in-process
  one. Agents are still declared as `AgentHandle(name, run_stream)` and are
  still the plain AG-UI shape — the provider does the routing, which is where
  it belongs when one identity serves several agents.
- `thread_history.status` gains `cancelling`.
- **The migration chain is one revision again.** The baseline plus the three
  changes made within days of it were collapsed: souk has never been
  released, and a baseline that creates a column a later revision deletes
  costs more in confusion than the history is worth. A database created by
  the old chain has to be recreated — its `alembic_version` names a revision
  that no longer exists. Verified by building the schema both ways and
  comparing every column, constraint and index on both backends.

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
