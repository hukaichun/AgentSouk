"""What a provider is, and what souk asks of it.

One method. An agent is exactly what AG-UI says it is — a run input in,
events out — and the only addition is the *name*, on the method rather than
smuggled into the input, because AG-UI's RunAgentInput carries no agent
identity and one provider serving a translator and a summarizer has to know
which one a run is for.

The name rather than an id: within one provider a name is unique, and the
provider already knows its own key. souk mints no identifier for anyone to
hold — souk mints no id for a provider to keep.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


class Provider(Protocol):
    """One identity offering one or more agents."""

    def run_stream(self, agent_name: str, run_input: dict[str, Any]) -> AsyncIterator[Any]: ...


@dataclass(frozen=True)
class DeliveredRun:
    """One run handed to this provider — this package's own type, on purpose.

    The loop used to read `run.run_id`, `run.agent.name` and `run.run_input`
    straight off whatever souk delivered, which made souk's `ClaimedRun` part
    of this package's interface without either side saying so. It broke once
    exactly that way: souk handed over its dispatch object, whose input field
    is `input_json`, and the first real provider died with an AttributeError
    on its first run.

    So the loop reads this instead, and building one from whatever the other
    side actually sends is the integrator's job — the one place that is
    entitled to know both shapes. See `contract.py`.

    `agent_name` rather than the pair: within one provider a name is unique,
    and the provider already knows its own key.
    """

    run_id: str
    agent_name: str
    run_input: dict[str, Any]
    thread_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


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
