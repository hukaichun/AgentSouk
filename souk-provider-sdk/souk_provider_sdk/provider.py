from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ag_ui.core import RunAgentInput


class Provider(Protocol):

    def run_stream(self, agent_name: str, run_input: RunAgentInput) -> AsyncIterator[Any]: ...


@dataclass(frozen=True)
class DeliveredRun:
    """The run data handed to a `SoukLink`, translated from souk's internal claimed-run representation."""

    run_id: str
    agent_name: str
    run_input: RunAgentInput
    thread_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentHandle:

    name: str
    run_stream: Callable[[RunAgentInput], AsyncIterator[Any]]
    description: str = ""
    agent_card_extra: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_registration(self) -> dict[str, Any]:
        registration: dict[str, Any] = {"name": self.name, "description": self.description}
        if self.agent_card_extra:
            registration["agent_card_extra"] = self.agent_card_extra
        if self.metadata:
            registration["metadata"] = self.metadata
        return registration


class HandleProvider:
    """A `Provider` that dispatches `run_stream` by agent name to the matching `AgentHandle`'s callable."""

    def __init__(self, agents: list[AgentHandle]) -> None:
        self.agents = {agent.name: agent for agent in agents}

    def run_stream(self, agent_name: str, run_input: RunAgentInput) -> AsyncIterator[Any]:
        return self.agents[agent_name].run_stream(run_input)
