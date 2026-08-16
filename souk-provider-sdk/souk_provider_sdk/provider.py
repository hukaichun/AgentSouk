"""What a provider is, and what souk asks of it.

One method. An agent is exactly what AG-UI says it is — a run input in,
events out — and the only addition is the *name*, on the method rather than
smuggled into the input, because AG-UI's RunAgentInput carries no agent
identity and one provider serving a translator and a summarizer has to know
which one a run is for.

The name rather than an id: within one provider a name is unique, and the
provider already knows its own key. souk mints no identifier for anyone to
hold (see docs/retiring-agent-id.md).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol


class Provider(Protocol):
    """One identity offering one or more agents."""

    def run_stream(self, agent_name: str, run_input: dict[str, Any]) -> AsyncIterator[Any]: ...


@dataclass
class AgentHandle:
    """One agent, declared the way AG-UI defines one."""

    name: str
    run_stream: Callable[[dict[str, Any]], AsyncIterator[Any]]
    description: str = ""

    def as_registration(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description}


class HandleProvider:
    """A provider that routes by name to the handles it was given.

    The routing every provider serving several agents needs, done once here
    rather than in each agent. Subclass or replace `run_stream` to route
    differently — a dynamic roster, a shared model pool, a dispatch table of
    your own — and nothing else changes.
    """

    def __init__(self, agents: list[AgentHandle]) -> None:
        self.agents = {agent.name: agent for agent in agents}

    def run_stream(self, agent_name: str, run_input: dict[str, Any]) -> AsyncIterator[Any]:
        return self.agents[agent_name].run_stream(run_input)
