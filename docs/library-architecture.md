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
  worker.py           #   the loop that claims work and drives a provider
  repo.py schema.py   #   the database, and the only thing core knows about
  protocols/          #   ② agui, a2a, kyok — pure translation, no I/O
souk_server/          # ③ the reference gateway — its own repository
                      #   (AgentSoukServer), consuming souk via a submodule
  server.py           #   process bootstrap, CORS, TLS, one listener
  api_*.py deps.py    #   FastAPI wiring of souk.protocols, and the models
  ws_*.py             #   the worker + KYOK channels (the NAT-traversal
                      #   relays) — WebSockets on the same listener
```

`souk` depends on SQLAlchemy (plus a driver), pydantic-settings,
ag-ui-protocol, cryptography, pyjwt and alembic — and on no transport at all.
It cannot import `fastapi`, `uvicorn`, `sse-starlette`, `grpcio` or
`websockets`, because none of them is installed with it: that is the invariant this whole design
exists to protect, and packaging is what makes it structural rather than a
matter of discipline. A test enforces it too (see souk/tests/
test_core_is_network_free.py) — packaging protects against the accident, not
against someone adding the dependency back on purpose.

The gateway is not an extra of `souk` — it began as a sibling subproject
here and is now its own repository entirely (AgentSoukServer, which pins
this one as a submodule). A separate distribution makes the boundary
impossible to erode by accident: core cannot grow a `uvicorn` import
without someone noticing they're in the wrong package. A separate repo
goes further — network design cannot even *originate* here (issue #27).

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
asking. The gateway maps them onto `/healthz` (answers from nothing, so a
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

**The gateway (AgentSoukServer)** — every field that only means something once there is a
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

### Attaching providers: souk hands work over, and takes an ack

`Souk.attach_provider(provider, agent_names)`. `provider` is anything
satisfying `broker.ConnectedProvider` — a public key, `deliver`, `cancel` —
and what carries those is not core's business and does not appear:

```python
class ConnectedProvider(Protocol):
    public_key: str
    max_concurrent_runs: int | None
    async def deliver(self, run: ClaimedRun) -> bool: ...
    def cancel(self, run_id: str) -> None: ...
```

**Only the broker's loop delivers.** `run_forever` is the one caller of
`_offer_pending`, and that is load-bearing rather than tidy: the queue's head
is read, the provider awaited, and the run removed only afterwards — so two
callers both hand out the same run, and the second `popleft` removes the
*next* one along, which is thereby lost rather than delayed. `expire_queued`
only looks at the pending queue, so a run taken out of it and given to nobody
is never offered again and never given up on either. Enqueueing a run and
registering a provider therefore set an event and hand out nothing
themselves.

**Delivery is the hand-over.** `ClaimedRun` carries the input, so there is no
second call in which souk supplies it and no window where a provider holds a
run and is still waiting to be told what to do. It is deliberately *not*
`broker.Run`: that is souk's own dispatch state with the run's queues
attached, which a provider could reach into in-process and could not be given
over a wire at all.

**The ack is the provider saying it has the run.** True and the run is
running, recorded in the same synchronous step it is handed over, so nothing
can observe a run that has been given away and belongs to nobody. False, an
exception, or silence past `deliver_timeout_seconds` leaves it exactly where
it was — queued, to be offered again. A provider that answers after souk gave
up is accepted (`accept_late_ack`) rather than having its run delivered
twice.

**Capacity is declared, and corrected by being refused.** souk cannot see
inside a provider, so `max_concurrent_runs` is a claim: souk keeps a bucket
that size, one per identity however many agents it serves, and refills it
from the run terminations it already observes. A provider that declines while
souk believed it had room is recorded `misdeclared` and treated as full from
then on — believe the provider, and record that souk had to find out by being
refused. `quality()` exposes that alongside `abandoned`, `unanswered` and
`answered_late`: things souk saw, never things it inferred.

**Reporting is authorized.** Every event names the identity that took the run
and is checked against it. Holding a connection is not the same as holding a
particular run.

**`Souk.start()` is required.** The loop it begins is the only thing that
hands a run to anybody, so a souk that was never started accepts every run
and dispatches none. `enqueue_run` refuses rather than queueing into a loop
that never turns, and `Health.ready` counts dispatch.


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
when it took the run (`ConnectedProvider.cancel`), invoked once and not awaited,
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

souk's own data is modeled, not passed around as bare `dict`. The query
methods are now: `list_agents` returns `AgentSummary`, `get_agent` an
`AgentRecord`, `get_run` a `RunRecord` (`souk/models.py`).

The reason is not annotations. souk's field names were only ever written down
in `repo.py`'s row-building, so every consumer learned them by reading it —
the gateway carried a hand-written model listing exactly those keys, kept in
step by nobody. Naming them where the data is produced means a rename breaks
at the source instead of downstream, and `tests/test_query_models.py` pins
the field *sets* whole, so a disappearing field fails a test rather than a
consumer.

`get_run` got narrower in the process. Runs live in `thread_history` next to
messages, and it was `select(thread_history)`, so it also returned `id`,
`kind`, `message_id` and `message_json` — the columns that make that sharing
work, and meaningless as facts about a run. Nothing read them (checked across
the repo rather than assumed).

Still bare `dict`: `get_thread_tree` and `get_thread_snapshot`, whose shapes
are nested and ad-hoc with nothing duplicating their contract, and
`build_run_agent_input`, which validates into a real
`ag_ui.core.RunAgentInput` and then `.model_dump()`s it — one validation at
the boundary, then the checked payload travels as data.

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

### A display name is not an address, so core stopped resolving one

Both protocol adapters used to carry a `resolve_agent(name)` — a bare display
name in, one agent out, `AgentNotFound` for none and `AmbiguousAgentName` for
several. It existed to serve `POST /agui/{name}` and `/a2a/{name}/...`, and
it is gone, along with `Souk.resolve_agents_by_name`, its repo query and the
error.

The AG-UI half had never worked. It returned `candidates[0]["agent"]`, and
the row it indexes has no `agent` key — `provider_key, name, agent_card,
metadata, joined_at, last_seen_at` — so it raised `KeyError('agent')` on
every name including the one-match happy path (#41). Its A2A sibling was
carried across correctly in the same change.

Pulling on that found the larger fact: **`AGUIAdapter` had no test at all**,
and its other method was dead too. `run` opened with
`repo.get_agent_by_id(session, agent)`, a function that does not exist in
`repo` — `AttributeError` on line one, every call. Worse than the name was
the shape: it rebound `agent` to the record it expected back, while
`ensure_thread`, `create_run` and `enqueue_run` all take an `AgentRef`. That
would have passed the wrong type to three call sites, and only escaped
notice at the fourth because `AgentRecord` happens to carry the same two
field names. The lookup is an existence check, so it no longer rebinds
anything, and `is_serving(agent)` stopped rebuilding a ref it was already
holding.

`A2AAdapter` is used in thirteen places across two test modules and was
fine. `AGUIAdapter` was imported by nothing, and both of its methods were
broken — which is the whole lesson, and why `tests/test_agui_adapter.py`
now exists. A green suite says nothing about code no test imports.

The reason to delete rather than fix is that no protocol asks for it.
AG-UI's `RunAgentInput` has no agent field at all — `thread_id, run_id,
parent_run_id, state, messages, tools, context, forwarded_props, resume` —
so which agent a request means always comes from outside the body, and since
`ServedInterface` moved route layout out of core (see its note), what the
path segment *is* became the gateway's choice. A gateway that puts the pair
in the URL needs no resolution, and a standard client neither knows nor
cares: it uses the URL it was handed.

That leaves resolution as a convenience for humans typing URLs, bought at the
price of a second way to address an agent — one that can return the wrong
agent's answer the moment two providers pick the same name, which
`register_agents` explicitly permits. A gateway that wants friendly URLs can
still keep its own name table; what it must not do is push the guess down
into core, where the pair is the identity and there is no other.

Browsing for who offers a name is `list_agents`, which answers with all of
them and asks the caller to choose.

### The provider SDK names nothing of souk's, and one adapter joins them

`souk_provider_sdk` could not import souk — that was the boundary — but it
reached the other way freely, and nothing recorded that it did. The loop read
`run.run_id`, `run.agent.name` and `run.run_input` off whatever souk
delivered, and called `souk.report_event` / `souk.finish_run` by name. Souk's
model fields, method names and argument order were all part of that package's
interface with neither side declaring it, and it broke exactly there: souk
handed over its own dispatch object, whose input field is `input_json`, and
the first real provider died with an `AttributeError` on its first run.

An import graph is not a boundary. What made the coupling invisible is that
it never showed up as a dependency — the package's own `pyproject.toml`
proved it did not depend on souk, and it was still wired into souk's API
shape at four call sites.

There are three kinds of agreement here and only two of them are removable:

- **API** — calling souk's methods. Removed. Results now leave through
  `on_event(run_id, event)` and `on_finish(run_id)`, two synchronous
  callables the caller supplies.
- **Type** — reading souk's objects. Removed. Runs arrive as `DeliveredRun`,
  the SDK's own frozen dataclass.
- **Protocol** — the registration signing payload in `identity.py`. *Not*
  removable, and not coupling: those bytes must match souk's verifier exactly
  or nothing can register at all. It is a wire format both sides implement,
  stated independently on each side rather than shared, because something
  derived from souk agrees with souk by construction and checks nothing.

What is left is one class, `souk_provider_sdk.SoukConnection`, and the
translation happens there once for every transport that will ever exist:

    souk's ClaimedRun ──▶ DeliveredRun ──▶ however this transport carries it

`deliver` is concrete and holds every souk field name this package depends
on. Subclasses implement `offer(DeliveredRun) -> bool` and `cancel(run_id)`,
and declare `public_key` and `max_concurrent_runs` — nothing else differs
between a function call and a socket. `InProcessProvider` is one
implementation; a gateway's `SocketProvider`, which answers `offer` with a
frame and an ack, is another. In-process is a transport, not a special case.

Only the souk-facing half is in the base. Reporting events back is not, and
cannot be: in-process the runtime is right there and its callbacks go
straight to souk, but over a wire the connection souk talks to lives in the
gateway while the runtime is on the far side of the socket. A base covering
both directions would fit exactly one of them.

It ships from **the SDK**, and the reason is a dependency asymmetry rather
than a preference. Nothing in it imports souk — every souk name is reached by
attribute — so putting it here costs nothing and the SDK's `pyproject.toml`
stays `cryptography` + `pyjwt`, which is the evidence that the boundary is
real. Putting it in souk was tried first and does not have that property:
building a `DeliveredRun` needs the class, so souk would take a real
dependency on the SDK to ship it. Zero against one is not a trade.

An abstraction with one implementation is a class with extra steps, so the
SDK's suite writes a second one — a queue with an ack, holding no runtime,
which is the shape a gateway-side connection has — and drives the base
through it (`souk-provider-sdk/tests/test_connection.py`).

`souk_provider_sdk/contract.py` states the shapes the adapter must satisfy —
`DELIVERED_RUN_FIELDS`, `REPORT_CALLBACKS`, `CONNECTED_PROVIDER_ATTRS` — and
souk's suite asserts they still hold, so a change on either side fails at
merge time rather than at a customer. That check earned itself immediately:
`ConnectedProvider` is *four* things, not the three its own docstring claims,
and the first adapter omitted `max_concurrent_runs` — which constructs and
attaches cleanly, then fails inside the broker at registration.

### souk has an identity too, and neither side generates one

Authentication ran one way. A provider *is* its Ed25519 keypair and proves
that on demand; souk held no key at all — its only secret is
`token_signing_secret`, which is symmetric and therefore cannot prove
anything to somebody who does not already hold it. So a provider connected to
a URL and trusted whatever answered.

TLS does not close that, and the reason matters because it recurs. TLS
authenticates a *hostname*, and in an enterprise it routinely terminates at an
intercepting proxy whose CA the endpoints have been made to trust — that is
the proxy's whole function, not a defeat of it. "The same souk as last time"
and "a host holding a valid certificate for this name" are different claims,
and only the first is what a provider needs. So souk gets a keypair of its
own (`SoukIdentity`), and both sides gain a general `sign(payload: bytes)`:
core already published `verify_signature` over arbitrary bytes, so each side
could check anyone while being uncheckable itself.

**What gets signed is not core's to decide.** Proving identity as a
connection opens is a serving act, so the payload belongs to whoever serves
souk (#27), and core supplies only the primitive. The handshake built on it —
challenge-response over a nonce each way, replacing a self-signed assertion
that was replayable within its freshness window — is designed in issues #44
and #45.

Two decisions worth keeping, because both will be re-proposed:

- **Channel binding is out.** It is the standard answer to a relay, and it is
  unusable here: an intercepting proxy re-originates TLS by design, so the two
  sides never derive the same value and the check fails for every enterprise
  running one. It was also never the fix — challenge-response already stops
  the credential being *stealable*, with such a proxy in the path. What stays
  open is tampering on a live connection, deliberately: the proxy is in the
  trust model by construction, and signing every frame is a large cost against
  a threat the operator chose.
- **An absent key is not a generated key.** `identity_private_key` is
  optional and souk never mints one for itself. An ephemeral identity would
  change on every restart and fail every provider's pin, which trains people
  to click through the warning and destroys the only thing pinning is worth.
  It is a hex value rather than a path for the same reason
  `token_signing_secret` is: every replica of one souk must present the same
  identity, and it must survive restarts, so it is something to provision.
  `SoukIdentity.generate_hex()` exists for whoever provisions it and is called
  by nothing.

### In-process is not trusted

The first version of attaching an agent put an object in a dictionary. That
was a side door past two things souk otherwise insists on, and both had real
consequences:

- **Anything holding the Souk could serve any agent**, with no proof.
  Sharing a process is not a reason to be trusted; a provider's identity is
  its Ed25519 keypair whether or not there is a wire in between.
- **souk had no idea it was there.** The dictionary was invisible to the
  liveness model the roster and the offline fast-fail actually read, so an
  attached provider reported `online: False` and calls to it failed with
  "agent is currently offline" while it sat right there. Observed, not
  hypothetical.

Both go through the same door, and it is literally the same call:

| | remote | in-process |
|---|---|---|
| proves identity | signed registration | the same signed registration |
| takes work | `deliver`, and acks | `deliver`, and acks |
| says "still here" | souk holds it (`RunBroker.serving`) | the same |
| souk learns it left | the gateway unregisters it | `detach_provider` |

`Souk.register_agents` verifies the signature and timestamp freshness — that
check used to live in the HTTP router even though registering is a domain
act, identical regardless of transport. `attach_provider` refuses a name this
key never registered, so registering is a prerequisite in-process exactly as
it is remotely, and every event a provider reports is checked against the run
it was actually given.

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

unsubscribe = souk.on_change(callback)       # told, rather than asking again
souk.active_runs()                           # live in-memory dispatch state
```

### Being told, instead of asking again

`on_change` is the read side's counterpart to `on_cancel`: a plain
synchronous callback, invoked and not awaited, no queue, no replay, no
ordering guarantee. souk already computes these facts — an agent registered,
a run went to `running` — and the only way to learn one used to be asking
again on a timer.

Deliberately coarse. `RosterChanged` says *something* is different, not what;
the subscriber re-queries. Fine-grained events would be a promise of a
complete, ordered account of every change, which souk cannot keep across a
restart or a second process.

Two details worth stating because they are the ones that would otherwise be
discovered:

- **`Souk.mark_run_status` is the only way a run's status moves.** Seven call
  sites do it today, and the eighth — added later, by someone who has not
  read this — is the one that would silently not notify. `repo` keeps the
  storage half, and `tests/test_change_hook.py` walks the AST to assert
  nothing outside `Souk` calls it.
- **Going offline by falling silent fires nothing.** `online` is derived —
  `last_seen_at` against a window, evaluated when you ask — so there is no
  instant at which anything happens. Registration, attach and detach all
  fire; staleness is poll-only, and says so in `changes.py`.

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

## Dispatch has been inverted twice, and the reasons differ

Core called a provider and pulled its events; then a provider claimed work
and pushed back; now souk delivers and the provider acks. Both inversions
were argued from measurements, and the record of each is in the git history
rather than here — what matters for reading the code is which problem each
shape does *not* have.

**What calling out cost.** A single event crossed three queues and two
routing tables, one of them transport-side and keyed by run_id. Claiming
removed the second table, and pushing keeps it removed: a provider is handed
a run and reports against it directly.

**What claiming cost.** souk could not tell a provider anything, because
providers only ever asked and souk only ever answered `[]`. That is issue
#37: "nothing queued" and "you own none of these" were the same reply, and a
provider waited 30 minutes on the second one looking healthy. Delivering asks
the ownership question once, at `attach_provider`, before any run exists.

**What delivering costs, and this is the live one.** Connection affinity.
"A worker is not dispatched *to*; it comes and claims, over a plain call any
node can answer" was true and is no longer: a run created on node A for a
provider attached to node B has to reach B. See below.

**Liveness stopped being an inference along the way.** "Claiming marks it
seen" was the whole model, deliberately without a heartbeat, because asking
for work produced the fact. Nothing asks now, so nothing produces it — and
souk does not need it to, because it holds the provider object.
`RunBroker.serving(agent)` is a fact. This was measured before it was
changed: with `online` still derived from `last_seen_at`, an attached
provider that had just completed a run was reported offline sixty seconds
after attaching.

## What this leaves open: horizontal scaling

Not a goal now, but the layering should not foreclose it.
`docs/broker-horizontal-scaling.md` is the plan.

Where souk's state lives:

| Durable, already shared | Live, per-process |
|---|---|
| agent roster, threads, messages | `RunBroker._runs`, `_pending_by_agent` |
| run status, `run_events` | each `Run`'s `in_queue` / `out_queue` |
| | `RunBroker._providers` — who is reachable |
| | `_capacity` — what each has in flight |
| | the KYOK bridge registry (same shape) |

The database is the durable record and is deliberately *not* on the live
event-relay hot path, so dispatch runs on plain asyncio primitives.

**The one seam that has to move is reachability.** Every read of the provider
mapping goes through `RunBroker.serving` / `agents_served_by`, and the dict
is private, so answering across processes is one implementation rather than a
sweep through core. That answer is not extra work invented for the roster:
multi-broker delivery needs a shared record of which node holds which
connection anyway, and "is *any* node serving this agent" — which is what
`online` means — falls out of it.

It will need an expiry, because a row saying node B serves an agent outlives
node B being killed. `last_seen_at` becomes that expiry, written by whoever
holds the connection rather than by a provider asking for work.

**Provider-side scaling is deliberately not souk's problem.** A provider runs
as many processes as it likes; each attaches, each declares its own capacity,
and souk offers to whichever is serving the agent.


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
  regenerated on demand while the gRPC wire existed; the wire, its stubs
  and its tooling are gone now, but a protobuf major in either direction
  can bite any remaining consumer again.

None of the SDK's client, server or transport code is imported; those live
behind extras (`http-server`, `grpc`, `fastapi`) that are deliberately not
requested. `a2a.types.a2a_pb2` imports nothing from the forbidden list, which
is what makes this survivable at all.

**Where this rule is not yet applied:** A2A also defines a gRPC binding, and
souk serves A2A over JSON-RPC only. If that changes, the stubs must come from
`a2a-sdk`, not from a hand-written `.proto`. souk's own worker channel is a
different hop entirely — provider-to-souk work claiming, which A2A has no
concept of — and its frames (authored in the gateway repo's
`docs/server-mode.md`; formerly `proto/souk.proto`, retired with the gRPC
carrier) carry opaque `json_payload`, so they duplicate no A2A type.

## What the gateway is

The reference gateway, assembled from the above and owning every I/O
decision: reads `SOUK_*` env vars into `CoreSettings` + its own
`ServingSettings`, applies CORS, binds its listeners, terminates TLS, runs
the relay. Behaviour is what the `souk-server` console script always did;
what changed is where it ships: first a sibling distribution in this repo,
now its own repository — AgentSoukServer, where all serving design lives.

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

## KYOK splits the same way, for a structural reason

KYOK (`souk/kyok.py`, `api_llm_bridge.py`) lets a caller keep its own LLM
credentials: the caller's own bridge is what actually calls the model, so the
caller decides the key *and* the model/endpoint, while the provider only ever
sees an OpenAI-compatible URL.

Mechanically it is a **second broker**, structurally identical to the run
broker that already lives in core:

| KYOK | run equivalent |
|---|---|
| `GET /kyok/poll` | waiting for work to arrive |
| `POST /kyok/v1/chat/completions` | submitting a unit of work |
| `POST /kyok/respond/{id}` | streaming the result back |
| `_DONE` sentinel | `END_OF_STREAM` |
| its local queued timeout | `RunBroker.queued_timeout_seconds` |

KYOK still *polls*, and that is a real difference now that runs do not: the
caller's bridge reaches souk rather than souk reaching it, so souk has
nothing to deliver to. Whether it should be inverted the same way has not
been asked. `token_signing_secret` signs its token and nothing else, which is
the whole of what is left of that key's job.

So it splits exactly like runs do, and gets its own port:

```python
class LLMBridge(Protocol):
    """Whoever answers a completion request — the caller's own bridge."""
    async def complete(self, request: CompletionRequest) -> AsyncIterator[Chunk]: ...
```

- **in-process** — call a local LLM client directly; no HTTP anywhere
- **HTTP** (the gateway) — today's three `/kyok/*` endpoints

Token minting/verifying (`kyok.py`) is core: it signs with
`token_signing_secret`, which core already holds, and the `agent_id` it
carries is checked against the broker's live view of who is running that
`run_id` right now — a domain judgement, not a transport concern.

Partly done already: `KyokBridge` and `PendingCompletion` moved from
`api_llm_bridge` into `souk/kyok.py` when live dispatch state stopped being a
module singleton, since the bridge is structurally a second broker and is
held per-Souk for the same reasons. What remains for step 5 is the three
`/kyok/*` endpoints and the `LLMBridge` port.
