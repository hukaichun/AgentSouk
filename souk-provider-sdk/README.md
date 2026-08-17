# souk-provider-sdk

What a provider and souk agree on, stated from the provider's side: an
identity and what it signs, the port an agent implements, and the provider's
own worker loop.

**It carries no transport.** Its dependencies are `cryptography` and `pyjwt`
— no `httpx`, no `websockets`, no `grpcio`, and no `souk`. Wrapping the calls
in a network is a downstream job, and that dependency list is what makes the
boundary checkable rather than a matter of discipline.

## Which SDK is this

Three names are in circulation, and they are not the same package:

| Package | Repo | What it is |
|---|---|---|
| **`souk-provider-sdk`** | **here** | **the interaction itself — identity, the port, the loop. No transport.** |
| `souk-agent-sdk` | [AgentSoukServer](https://github.com/hukaichun/AgentSoukServer) | a Python client for the gateway's provider WebSocket |
| `souk-client-sdk` | AgentSoukServer | the caller's side |

If you are running an agent against a deployed gateway over a network, you
want `souk-agent-sdk` and [AgentSoukServer's quick
start](https://github.com/hukaichun/AgentSoukServer). This package is what
you want when souk is a library in your own process, or when you are building
a binding of your own and need to know what the two sides actually agree on.

## An agent is one method

```python
class Greeter:
    async def run_stream(self, agent_name: str, run_input: dict):
        ids = {"threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "RUN_STARTED", **ids}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "hello"}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", **ids}
```

A run input in, AG-UI events out — exactly what AG-UI says an agent is. The
only addition is `agent_name`, on the method rather than smuggled into the
input, because `RunAgentInput` carries no agent identity and one provider
serving a translator and a summarizer has to know which run is for which.

## This package names nothing of souk's

That is deliberate, and it is why there is an adapter below rather than a
one-line `serve()`.

Runs arrive as `DeliveredRun` — this package's own type — and results leave
through two callables you supply. Souk's model fields, method names and
argument order are not this package's business. It used to be otherwise: the
loop read `run.run_id` / `run.agent.name` / `run.run_input` off souk's object
and called `souk.report_event` / `souk.finish_run` by name, with nothing on
either side declaring it, and it broke exactly there — souk handed over its
dispatch object, whose input field is `input_json`, and the first real
provider died with an `AttributeError` on its first run.

The one agreement that cannot be removed is the **signing payload** in
`identity.py`: those bytes must match souk's verifier exactly or registration
fails. That is a wire format both sides implement, not a dependency either
way — and it is stated here independently rather than imported, because
something derived from souk agrees with souk by construction and therefore
checks nothing.

## The souk-facing side is a class you subclass

souk's broker knows a provider by four things and nothing about what carries
them. `SoukConnection` is that, from the provider's side, with the one step
every transport would otherwise repeat done once:

```
souk's ClaimedRun ──▶ DeliveredRun ──▶ however this transport carries it
```

`deliver` is concrete and holds **every souk field name this package depends
on**. A transport implements only what is actually different:

```python
class InProcessProvider(SoukConnection):     # ships here — a function call
    async def offer(self, run: DeliveredRun) -> bool:
        return await self._runtime.deliver(run)

class SocketProvider(SoukConnection):        # a gateway — a frame, and an ack
    async def offer(self, run: DeliveredRun) -> bool:
        self._outbound.put_nowait(encode(run))
        return await self._ack(run.run_id)
```

plus `cancel(run_id)`, `public_key` and `max_concurrent_runs`. All four are
abstract, `max_concurrent_runs` included: souk sizes its capacity bucket from
it, so a connection that omits it starves or overruns — better to fail at
construction than inside souk's broker at registration.

**In-process is a transport, not a special case.** Nothing it gets is a
shortcut a remote provider does not: it registers, it proves its identity,
and souk offers it work the same way.

Only the souk-facing half is here. Reporting events back is not, because it
is not always the same object's job — in-process the runtime is local and its
callbacks go straight to souk, but over a wire the connection souk talks to
lives in the gateway and the runtime is across the socket.

## Using the in-process one

```python
from souk_provider_sdk import InProcessProvider

runtime = ProviderRuntime(identity, provider)
runtime.start()
await souk.attach_provider(InProcessProvider(souk, runtime), ["greeter"])
```

It adds no dependency: souk is never imported, only duck-typed, so this
package's requirements stay `cryptography` + `pyjwt`. It lives here rather
than in souk for that reason and not a taste one — souk shipping it would
mean souk importing this package to build a `DeliveredRun`.

## In-process, end to end

```python
from souk.config import CoreSettings
from souk.core import Souk
from souk_provider_sdk import (
    InProcessProvider, ProviderIdentity, ProviderRuntime,
)

souk = Souk(CoreSettings(database_url=..., token_signing_secret=...))
await souk.start()

# 1. Identity is a keypair. Nothing is issued to it and there is no id to hold.
identity = ProviderIdentity.generate()          # or .load_or_create(path)

# 2. Register the names, signed. Sharing souk's process is not a reason to
#    skip this — see "In-process is not trusted" in docs/library-architecture.md.
signature, timestamp = identity.sign_registration(["greeter"])
registered = await souk.register_agents(
    identity.public_key, signature, timestamp, [{"name": "greeter"}]
)
agent = registered.agents["greeter"]            # an AgentRef

# 3. The loop is yours. Start it, then put a connection in front of it.
runtime = ProviderRuntime(identity, Greeter(), max_queued_runs=4)
runtime.start()
await souk.attach_provider(InProcessProvider(souk, runtime), ["greeter"])

assert souk.is_serving(agent)

# 4. Reachable.
handle = await souk.start_run(agent, {"messages": [{"role": "user", "content": "hi"}]})
async for event in handle.events():
    print(event["type"])
```

Registration and attaching are two steps because they answer two questions:
registration is what makes the name souk's to serve, attaching is what makes
it reachable. A registered agent nobody serves is `online: False`, not an
error.

## Several agents on one identity

```python
from souk_provider_sdk import AgentHandle, HandleProvider

provider = HandleProvider([
    AgentHandle(name="translator", run_stream=translate, description="translates"),
    AgentHandle(name="summarizer", run_stream=summarize, description="summarizes"),
])

signature, timestamp = identity.sign_registration(list(provider.agents))
await souk.register_agents(
    identity.public_key, signature, timestamp,
    [h.as_registration() for h in provider.agents.values()],
)
```

`HandleProvider` is name-routing done once instead of in every agent. Replace
`run_stream` to route differently — a dynamic roster, a shared model pool, a
dispatch table of your own — and nothing else changes.

One `ProviderRuntime` per provider, never per agent: its capacity is a budget
across everything it serves, exactly as one process is.

## Capacity is something you say, not something souk measures

souk hands work over; it does not wait to be asked. The broker offers each run
to whoever serves its agent, and `deliver` is where that lands:

- returning **True** is the ack — souk records the run as started from there;
- returning **False** leaves it queued for someone to take later.

Declining is the only channel capacity has. `max_queued_runs` (how many may
wait) and `max_concurrent_runs` (how many may run) are what make `deliver`
say no. Both are deliberately small by default — a deep queue looks like
throughput and is really a promise you have not checked you can keep, and
souk would believe every one of those runs had started.

## Cancelling is a request

`runtime.cancel(run_id)` is souk asking a run to stop. Complying is the
provider's choice; this implementation complies by cancelling the task, which
is the only way to interrupt an arbitrary async generator. A run that ignores
it and finishes has finished, and souk records that — souk never records an
outcome it has not observed.

## Shutdown is two calls, and one is easy to miss

```python
await runtime.aclose()                              # stop the loop
assert souk.is_serving(agent)                       # ← still True
await souk.detach_provider(identity.public_key)     # take it off the roster
assert not souk.is_serving(agent)
```

`aclose` stops this provider's loop and tells souk nothing. Reachability
lives in the broker, so `detach_provider` is what makes the agent go offline
at once rather than ageing out of the liveness window.

`aclose(cancel_in_flight=True)` picks the other shutdown: draining lets souk
see each run's real outcome, cancelling makes each stream end without a
`RUN_FINISHED`, which souk records as failed unless it had already asked for
a stop. Neither is a lie — they are different shutdowns.

## What souk has to be

`ProviderRuntime` takes no souk at all. Results leave through `on_event` and
`on_finish`, two synchronous callables — synchronous because an agent must
never wait on whatever is downstream, and because `on_finish` is called while
unwinding a cancellation, where an await would be interrupted before it
arrived.

Inbound, a caller needs four things from the runtime: `public_key`,
`max_concurrent_runs`, `deliver`, `cancel`. Souk's `ConnectedProvider` is the
same four — which is why the adapter above is thin — and `contract.py` states
them as data so the other side can check rather than assume.

Ordering stays here. `_report_output` is a single consumer, so a run's events
leave in the order the agent produced them; handing out a stream to drain
would make that the caller's problem, and nothing would go red when two of
their tasks raced.

## Further

- `docs/agent-provider-guide.md` — the situations a live provider runs into:
  sub-agent calls that are still pending, session continuity, what souk does
  and does not do for you.
- `docs/library-architecture.md` — why the loop is the provider's, why
  in-process is not trusted, and where the core/serving boundary is.
- AgentSoukServer's `docs/server-mode.md` — the wire contract, if you are
  writing a binding rather than using one.
