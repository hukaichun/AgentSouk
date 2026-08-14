"""The port an agent reaches souk through.

This is deliberately *the AG-UI agent shape* — a run input in, a stream of
events out — rather than an interface of souk's own invention. AG-UI already
defines an agent that way, and it is what `pydantic_ai.ui.ag_ui.AGUIAdapter`
and `souk_agent_sdk`'s own `run_stream` already produce, so souk asks for the
thing that already exists instead of a parallel one (see
souk-no-forced-protocol-deviation, and docs/library-architecture.md for the
earlier four-method draft this replaces).

Nothing here is network-shaped. A provider may be a local Python agent with
no socket anywhere, a gRPC-connected remote agent, or — later — a peer souk
node holding that agent's connection; core cannot tell the difference, which
is the whole point.

Connection strategy is deliberately *not* part of this contract. `run` is
called once per run, but that says nothing about connections: a transport is
free to multiplex every run of every agent over one connection, which is
exactly what the gRPC implementation does.
"""

from __future__ import annotations

import inspect
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
class AgentProvider(Protocol):
    """Anything that can run an agent and stream back AG-UI events.

    Structural, so there is nothing to subclass and no adapter to write for
    the common case: an object whose `run` takes a RunAgentInput and yields
    AG-UI events already satisfies this.

    Two call shapes are accepted, and `open_run` below normalizes them:

    - a plain async generator — `def run(self, run_input) -> AsyncIterator`.
      The natural shape, and what an AG-UI adapter already is. Lazy: nothing
      happens until the stream is first iterated.
    - an async function returning one — `async def run(self, run_input)`.
      For transports that must do work *before* the first event is asked
      for, notably delivering the run input over a wire; see
      souk.grpc_server.GrpcProvider, which relies on that delivery having
      happened by the time this returns.
    """

    def run(self, run_input: dict) -> AsyncIterator[AgentEvent]: ...


async def open_run(provider: AgentProvider, run_input: dict) -> AsyncIterator[AgentEvent]:
    """Start `provider`'s run and hand back its event stream.

    Awaits the provider's own setup if it has any (the async-function shape
    above), so a caller can rely on "this returned" meaning "the run really
    has been handed over" — which is what makes it safe to cancel a run
    immediately afterwards without the agent being left waiting for an input
    that was never sent.
    """
    stream = provider.run(run_input)
    if inspect.isawaitable(stream):
        stream = await stream
    return stream
