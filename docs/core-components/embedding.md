# Embedding souk

Part of [core components](../core-components.md).

souk is a library before it is a service. Everything a serving layer
does over a wire, a Python process can do by calling methods — same
objects, same guarantees, no socket. This page is that surface.

```python
from souk.core import Souk
```

The package exports only `migrate` at the top level; `Souk` comes from
`souk.core`.

## The object

`Souk(settings=None, broker=None)` builds the engine and the dispatch
machinery. Both arguments are injection points: settings default to
`CoreSettings()` read from the environment (see
[Support](support.md)), and passing your own `RunBroker` is how you
change dispatch timeouts, which are broker constructor arguments rather
than settings.

`await souk.start()` must be called before any run is enqueued —
enqueueing on a stopped broker raises rather than silently accepting
work nothing would dispatch. It returns the ids of runs it reaped: runs
left `queued` or `running` by a previous process, which no longer have
anyone to finish them. `await souk.aclose()` stops dispatch, cancels
tracked background tasks, and disposes the engine.

`await souk.health()` is the readiness probe: database reachable, schema
at the expected revision, dispatch loop alive.

## Starting a run

```python
handle = await souk.start_run(agent, run_input, thread_id=None, metadata=None)
```

`agent` is an `AgentRef` — a `(provider_key, name)` pair, because a name
alone is not an identity. `run_input` is the AG-UI-shaped payload the
provider will receive. Omitting `thread_id` opens a new thread; passing
one continues it.

The `RunHandle` you get back carries `run_id`, `thread_id`, an
`async events()` iterator yielding each AG-UI event as the provider
produces it, and `cancel()`.

!!! warning "`RunHandle.is_live` carries no information"
    Every handle core constructs sets it `True`, and nothing ever
    constructs one with `False`. Do not branch on it. The
    live-versus-reconstructed distinction is real but lives in the A2A
    adapter, which decides it from its own start result plus a broker
    lookup — not from this field.

## Resuming and cancelling

```python
handle = await souk.resume_run(run_id, run_input, metadata=None)
```

A resumed run **keeps its id**. It is the same run being invoked again
with the caller's answer, not a successor — which is why an A2A task id
stays valid across a pause. An unknown run id raises `LookupError`.

`souk.cancel_run(run_id)` requests a cancel. It is synchronous and
returns whether the request was passed on, not whether the run stopped —
souk asks, and records only what it then observes. See
[runs and cancels are requests](../mechanisms/requests.md).

## Three ways to address a run

A run is reachable by whichever of these the caller still has, and the
query surface exists so that losing the handle is never losing the run:

- **By handle** — the live event stream, for as long as the process that
  started it holds the object.
- **By id** — `get_run(run_id)` for the record, and
  `get_run_events(run_id, since_seq=0)` for the persisted event log. The
  `since_seq` cursor is what lets a reconnecting reader resume mid-run
  instead of replaying from the start.
- **By thread** — `get_thread_messages(thread_id)` for the folded
  history, `get_thread_snapshot` for the thread as a caller reads it
  back, and `get_thread_tree` for the thread plus every delegated
  descendant nested under `children`.

`active_runs()` lists the run ids dispatch currently holds in memory.
That is live state, so it is the one query that says nothing about runs
this process did not dispatch.

## Watching for change

```python
unsubscribe = souk.on_change(callback)
```

Three event types exist, and they are deliberately coarse:
`RosterChanged()`, `LlmRosterChanged()` — neither carries fields — and
`RunStatusChanged(run_id, status)`. Events say *that* something changed;
reading the new state is the subscriber's own query.

Callbacks run **synchronously**, before the call that caused them
returns, and an exception a subscriber raises is logged and swallowed
rather than failing the operation that fired it. Keep them short.

## The roster

`list_agents()` and `list_llm_providers()` return what is registered,
each entry carrying `online` — meaning a provider is serving it right
now, which is a fact souk holds rather than an inference from a
timestamp. `is_serving(agent)` answers the same question for one agent.
Entries whose provider has not checked in within
`stale_hidden_window_seconds` are hidden from the listings.

## Design records

Why this is shaped the way it is, and what it was shaped like first:

- [Liveness stopped being an inference](../design-records.md#liveness-stopped-being-an-inference)
- [A provider is its key, and has no other id](../design-records.md#a-provider-is-its-key-and-has-no-other-id)
