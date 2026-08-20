from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from ag_ui.core import RunAgentInput
from pydantic import BaseModel, ConfigDict, Field


class Provider(Protocol):

    def run_stream(self, agent_name: str, run_input: RunAgentInput) -> AsyncIterator[Any]: ...


@dataclass(frozen=True)
class Refusal:
    """A permanent decline of an offered run: this provider will never accept it, so souk should stop re-offering and fail the run.

    `reason` is this provider's own words; souk records it verbatim on the
    run's failure record. Return one from `SoukLink.offer` instead of `False`
    (which means "full right now, offer again later"). souk reads the refusal
    duck-typed by its `reason` attribute — the attribute name is the
    contract."""

    reason: str


class DeliveredRun(BaseModel):
    """The run data handed to a `SoukLink`, translated from souk's internal claimed-run representation — and the declared wire form of an offered run.

    A transport carries exactly `model_dump(by_alias=True)` of this
    (camelCase keys, `runInput` as AG-UI's own camelCase form) and rebuilds
    it with `model_validate`; the canonical frame is published in
    `docs/contract-vectors.json`, so no transport hand-writes the mapping.
    `metadata` is part of the wire contract, defaulting to empty.
    `run_input.forwarded_props` is the caller's free-form slot, plus the two
    keys souk itself adds — `caller` and `kyok` — declared by this package's
    own `CallerProps` and `KyokForwardedProps` (`souk_provider_sdk.props`);
    validate with those rather than restating them. They are independent
    twins of souk's models, pinned to them by the delivered-run frame in
    `docs/contract-vectors.json`.
    """

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    run_id: str = Field(alias="runId")
    agent_name: str = Field(alias="agentName")
    run_input: RunAgentInput = Field(alias="runInput")
    thread_id: str | None = Field(default=None, alias="threadId")
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_claimed(cls, run: Any) -> "DeliveredRun":
        """Translates souk's claimed-run object (read by attribute, never imported) into the delivered form.

        Raises pydantic's `ValidationError` if the input doesn't validate as
        a `RunAgentInput` — a permanent condition, not a transient one.
        """
        return cls(
            run_id=run.run_id,
            agent_name=run.agent.name,
            run_input=RunAgentInput.model_validate(run.run_input),
            thread_id=run.thread_id,
            metadata=dict(getattr(run, "metadata", None) or {}),
        )


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
