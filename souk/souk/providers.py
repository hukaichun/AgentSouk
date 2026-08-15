"""The port a provider reaches souk through.

A **provider** is one identity offering one or more agents. That is what
souk has always meant by the word — registration is per provider and carries
a batch of agents (see `Souk.register_agents`), the roster groups by it, and
`souk-agent-sdk` is one process holding several `AgentHandle`s. This port is
the same thing for a provider that happens to live in souk's own process.

An **agent** is still exactly what AG-UI says it is: a run input in, a stream
of events out. The only thing added is which agent a run is for:

    async def run_stream(self, agent_id: str, run_input: dict) -> AsyncIterator[AgentEvent]

`agent_id` is on the method, not smuggled into the input, because AG-UI's
RunAgentInput carries thread and run ids and no agent identity, and souk does
not widen someone else's schema. Without it a provider serving a translator
and a summarizer cannot tell which of them a run is for — which made
in-process providers effectively single-agent, while the gRPC one papered
over the same gap with a private side-table.

Nothing here is network-shaped. A provider may be a local Python object with
no socket anywhere, or — over a wire — whatever `souk-agent-sdk` hosts; core
cannot tell the difference, which is the point.

What *drives* a provider is a worker loop (see souk/worker.py): it claims
runs and pushes their events back. Core never calls a provider itself. That
is the one thing the earlier version of this port got wrong — it had core
call `start()` and pull a generator per run, which cost an extra queue, an
extra routing table, and left an in-process provider with no way to say how
much work it could take.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

# Typed when a provider produces typed events — an in-process pydantic-ai
# adapter naturally does — and the original mapping when it is an event this
# version of souk does not recognize.
#
# Not narrowed to ag_ui.core's Event union on purpose. souk is a relay, and
# that union is discriminated on an `EventType` enum: an event type from a
# newer AG-UI than souk knows is *rejected outright*, which would break a run
# for no better reason than souk being a version behind. (Unknown *fields* on
# a known type are safe — ag_ui's models are `extra='allow'` and round-trip
# them intact.) Wrapping unknown events in ag_ui's `RawEvent` is not a fix
# either: its `type` is a hard-coded `Literal[EventType.RAW]`, so the caller
# would see `RAW` instead of the real new event type — corruption, not
# relaying. See docs/library-architecture.md for the measurements.
AgentEvent = Any


@runtime_checkable
class Provider(Protocol):
    """Anything that can run this identity's agents and stream AG-UI events.

    Structural, so there is nothing to subclass. A provider hosting one agent
    whose own `run_stream(run_input)` is already an async generator — what
    `pydantic_ai.ui.ag_ui.AGUIAdapter` and `souk_agent_sdk`'s `AgentHandle`
    produce — is a couple of lines:

        class Local:
            async def run_stream(self, agent_id, run_input):
                async for event in my_agent.run_stream(run_input):
                    yield event

    An async generator function, called and iterated directly — the same
    shape and the same call the SDK makes on the far side of a wire, so an
    agent moving between in-process and remote is the same object either way.

    Cancellation is deliberately not a method here. souk asks a *worker* to
    stop a run and the worker decides what that means (souk's own cancels the
    task running this generator, delivering CancelledError into whatever it
    is awaiting); a provider that wants to ignore the request and finish
    normally can, and souk records `completed` because that is what it
    observed. See docs/library-architecture.md on cancellation.
    """

    def run_stream(self, agent_id: str, run_input: dict) -> AsyncIterator[AgentEvent]: ...
