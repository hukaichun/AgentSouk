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

Connection strategy is deliberately *not* part of this contract. `start` is
called once per run, but that says nothing about connections: a transport is
free to multiplex every run of every agent over one connection, which is
exactly what the gRPC implementation does.
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
class AgentProvider(Protocol):
    """Anything that can run an agent and stream back AG-UI events.

    Starting a run is an explicit act — `start` returns only once the run has
    genuinely been handed over — and consuming its events is a separate one.
    That split matters because it is what actually happens: an agent behind a
    wire begins working the moment it receives its input and pushes events at
    its own pace, whether or not souk is reading. Modelling the handover as
    "iterate a lazy generator" would fuse the two and describe a pull that
    isn't real; it also means the input goes out only if and when somebody
    iterates, which leaves a cancel arriving first able to strand an agent
    waiting for input that was never sent.

    Structural, so there is nothing to subclass. A local AG-UI agent — whose
    own `run_stream(run_input)` is already an async generator — is wrapped in
    three lines:

        class Local:
            async def start(self, run_input):
                return agent.run_stream(run_input)
    """

    async def start(self, run_input: dict) -> AsyncIterator[AgentEvent]: ...

    async def cancel(self, run_id: str) -> None:
        """Ask the agent to stop. A request, not a command.

        souk does not enforce it and must not pretend to: the provider may
        honour it immediately, take a while, or ignore it and run to
        completion. Whatever it emits meanwhile is real output. What
        actually happened is read off the stream's ending, not assumed here
        — see souk.handlers._handle_finish.
        """
        ...
