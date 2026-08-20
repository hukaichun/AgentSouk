# funduq-provider-sdk

What a provider and funduq agree on, stated from the provider's side: an
identity and what it signs, the port an agent implements, and the provider's
own worker loop.

**It carries no transport.** Its dependencies are `cryptography`, `pyjwt` and
`ag-ui-protocol` — no `httpx`, no `websockets`, no `grpcio`, and no `funduq`.
Wrapping the calls in a network is a downstream job, and that dependency list
is what makes the boundary checkable rather than a matter of discipline.
`ag-ui-protocol` is there because a run input is a `RunAgentInput` and a
thread's messages are `ag_ui.core.Message` — the same package funduq itself
validates both against, not a network dependency.

## Which SDK is this

Three names are in circulation, and they are not the same package:

| Package | Repo | What it is |
|---|---|---|
| **`funduq-provider-sdk`** | **here** | **the interaction itself — identity, the port, the loop. No transport.** |
| `funduq-agent-sdk` | [funduq-server](https://github.com/hukaichun/funduq-server) | a Python client for the gateway's provider WebSocket |
| `funduq-client-sdk` | funduq-server | the caller's side |

If you are running an agent against a deployed gateway over a network, you
want `funduq-agent-sdk` and [funduq-server's quick
start](https://github.com/hukaichun/funduq-server). This package is what
you want when funduq is a library in your own process, or when you are building
a binding of your own and need to know what the two sides actually agree on.

## An agent is one method

```python
from ag_ui.core import RunAgentInput

class Greeter:
    async def run_stream(self, agent_name: str, run_input: RunAgentInput):
        ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
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

## This package names nothing of funduq's

That is deliberate, and it is why there is an adapter below rather than a
one-line `serve()`.

Runs arrive as `DeliveredRun` — this package's own type — and results leave
through two callables you supply. Funduq's model fields, method names and
argument order are not this package's business. It used to be otherwise: the
loop read `run.run_id` / `run.agent.name` / `run.run_input` off funduq's object
and called `funduq.report_event` / `funduq.finish_run` by name, with nothing on
either side declaring it, and it broke exactly there — funduq handed over its
dispatch object, whose input field is `input_json`, and the first real
provider died with an `AttributeError` on its first run.

The one agreement that cannot be removed is the **signing payload** in
`identity.py`: those bytes must match funduq's verifier exactly or registration
fails. That is a wire format both sides implement, not a dependency either
way — and it is stated here independently rather than imported, because
something derived from funduq agrees with funduq by construction and therefore
checks nothing.

## The funduq-facing side is a class you subclass

funduq's broker knows a provider by four things and nothing about what carries
them. `FunduqLink` is that, from the provider's side, with the one step
every transport would otherwise repeat done once:

```
funduq's ClaimedRun ──▶ DeliveredRun ──▶ however this transport carries it
```

`deliver` is concrete and holds **every funduq field name this package depends
on**. A transport implements only what is actually different:

```python
class InProcessLink(FunduqLink):     # ships here — a function call
    async def offer(self, run: DeliveredRun) -> bool:
        return await self._runtime.deliver(run)

class SocketClient(FunduqLink):          # a provider's own socket end
    async def offer(self, run: DeliveredRun) -> bool:
        self._outbound.put_nowait(encode(run))
        return await self._ack(run.run_id)
```

plus `cancel(run_id)`, `public_key` and `max_concurrent_runs`. All four are
abstract, `max_concurrent_runs` included: funduq sizes its capacity bucket from
it, so a connection that omits it starves or overruns — better to fail at
construction than inside funduq's broker at registration.

**In-process is a transport, not a special case.** Nothing it gets is a
shortcut a remote provider does not: it registers, it proves its identity,
and funduq offers it work the same way.

Only the funduq-facing half is here. Reporting events back is not, because it
is not always the same object's job — in-process the runtime is local and its
callbacks go straight to funduq, but over a wire the connection funduq talks to
lives in the gateway and the runtime is across the socket.

## Using the in-process one

```python
from funduq_provider_sdk import InProcessLink

runtime = ProviderRuntime(identity, provider)
runtime.start()
await funduq.attach_provider(InProcessLink(funduq, runtime), ["greeter"])
```

It adds no dependency: funduq is never imported, only duck-typed, so this
package's requirements stay `cryptography` + `pyjwt`. It lives here rather
than in funduq for that reason and not a taste one — funduq shipping it would
mean funduq importing this package to build a `DeliveredRun`.

## In-process, end to end

```python
from funduq.config import CoreSettings
from funduq.core import Funduq
from funduq_provider_sdk import (
    InProcessLink, ProviderIdentity, ProviderRuntime,
)

funduq = Funduq(CoreSettings(database_url=..., token_signing_secret=...))
await funduq.start()

# 1. Identity is a keypair. Nothing is issued to it and there is no id to hold.
identity = ProviderIdentity.generate()          # or .load_or_create(path)

# 2. Register the names, signed. Sharing funduq's process is not a reason to
#    skip this — see "In-process is not trusted" in docs/mechanisms/identity.md.
signature, timestamp = identity.sign_registration(["greeter"])
registered = await funduq.register_agents(
    identity.public_key, signature, timestamp, [{"name": "greeter"}]
)
agent = registered.agents["greeter"]            # an AgentRef

# 3. The loop is yours. Start it, then put a connection in front of it.
runtime = ProviderRuntime(identity, Greeter(), max_queued_runs=4)
runtime.start()
await funduq.attach_provider(InProcessLink(funduq, runtime), ["greeter"])

assert funduq.is_serving(agent)

# 4. Reachable.
handle = await funduq.start_run(agent, {"messages": [{"role": "user", "content": "hi"}]})
async for event in handle.events():
    print(event["type"])
```

Registration and attaching are two steps because they answer two questions:
registration is what makes the name funduq's to serve, attaching is what makes
it reachable. A registered agent nobody serves is `online: False`, not an
error.

## Several agents on one identity

```python
from funduq_provider_sdk import AgentHandle, HandleProvider

provider = HandleProvider([
    AgentHandle(
        name="translator",
        run_stream=translate,
        description="translates",
        # Merged into the agent's public card. `skills` is what discovery
        # searches on — an agent declaring none is findable only by somebody
        # who already knows its name.
        agent_card_extra={"skills": [
            {"id": "translate", "name": "Translate", "tags": ["language"]},
        ]},
        # funduq-internal, deliberately not on the public card.
        metadata={"cost_centre": "research"},
    ),
    AgentHandle(name="summarizer", run_stream=summarize, description="summarizes"),
])

signature, timestamp = identity.sign_registration(list(provider.agents))
await funduq.register_agents(
    identity.public_key, signature, timestamp,
    [h.as_registration() for h in provider.agents.values()],
)
```

`HandleProvider` is name-routing done once instead of in every agent. Replace
`run_stream` to route differently — a dynamic roster, a shared model pool, a
dispatch table of your own — and nothing else changes.

One `ProviderRuntime` per provider, never per agent: its capacity is a budget
across everything it serves, exactly as one process is.

## Capacity is something you say, not something funduq measures

funduq hands work over; it does not wait to be asked. The broker offers each run
to whoever serves its agent, and `deliver` is where that lands:

- returning **True** is the ack — funduq records the run as started from there;
- returning **False** leaves it queued for someone to take later.

Declining is the only channel capacity has. `max_queued_runs` (how many may
wait) and `max_concurrent_runs` (how many may run) are what make `deliver`
say no. Both are deliberately small by default — a deep queue looks like
throughput and is really a promise you have not checked you can keep, and
funduq would believe every one of those runs had started.

## Cancelling is a request

`runtime.cancel(run_id)` is funduq asking a run to stop. Complying is the
provider's choice; this implementation complies by cancelling the task, which
is the only way to interrupt an arbitrary async generator. A run that ignores
it and finishes has finished, and funduq records that — funduq never records an
outcome it has not observed.

## Shutdown is two calls, and one is easy to miss

```python
await runtime.aclose()                              # stop the loop
assert funduq.is_serving(agent)                       # ← still True
await funduq.detach_provider(identity.public_key)     # take it off the roster
assert not funduq.is_serving(agent)
```

`aclose` stops this provider's loop and tells funduq nothing. Reachability
lives in the broker, so `detach_provider` is what makes the agent go offline
at once rather than ageing out of the liveness window.

`aclose(cancel_in_flight=True)` picks the other shutdown: draining lets funduq
see each run's real outcome, cancelling makes each stream end without a
`RUN_FINISHED`, which funduq records as failed unless it had already asked for
a stop. Neither is a lie — they are different shutdowns.

## What funduq has to be

`ProviderRuntime` takes no funduq at all. Results leave through `report_event`
and `finish_run` on the same `FunduqLink` funduq hands runs to — a provider and a
funduq are one relationship, so they are one object. Both are `async`, so a
transport with a wire under it can await its write; the agent is decoupled by
the runtime's output queue, not by their arity.

Inbound, funduq needs four things from the link: `public_key`,
`max_concurrent_runs`, `deliver`, `cancel`. Funduq's `ConnectedProvider` is the
same four — which is why `InProcessLink` is thin — and `contract.py` states
them as data so the other side can check rather than assume.

Ordering stays here. `_report_output` is a single consumer, so a run's events
leave in the order the agent produced them; handing out a stream to drain
would make that the caller's problem, and nothing would go red when two of
their tasks raced.

## Further

- `docs/writing-a-transport.md` — the connect handshake in order, and what
  funduq deliberately leaves to you.
- `docs/mechanisms/requests.md` — what an offer's three-valued answer means,
  and what funduq records when a run ends.
- `docs/design-records.md` — why the loop is the provider's, why in-process
  is not trusted, and the decisions that were reversed getting here.
- funduq-server's `docs/server-mode.md` — the wire contract, if you are
  writing a binding rather than using one.
