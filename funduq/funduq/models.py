from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentRef(BaseModel):

    model_config = ConfigDict(frozen=True)

    provider_key: str
    name: str

    def __str__(self) -> str:
        return f"{self.provider_key[:16]}…/{self.name}"


class LlmRef(BaseModel):

    model_config = ConfigDict(frozen=True)

    provider_key: str
    name: str

    def __str__(self) -> str:
        return f"{self.provider_key[:16]}…/{self.name}"


class ClaimedRun(BaseModel):

    model_config = ConfigDict(frozen=True)

    run_id: str
    agent: AgentRef
    thread_id: str
    run_input: dict[str, Any]
    # Part of the delivered-run wire frame; funduq currently writes no keys
    # into it (a caller's addressing rides inside the run input itself, as the
    # message's own A2A `taskId`). The slot stays in the contract for
    # envelope-level data a future dispatch mechanism may need.
    metadata: dict[str, Any] = Field(default_factory=dict)


class LlmSummary(BaseModel):
    """A roster-list view of a registered LLM offering: enough to display and
    pick one (including online status), the mirror of `AgentSummary`."""

    provider_key: str
    name: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    joined_at: datetime
    last_seen_at: datetime
    online: bool = False
    provider_name: str | None = None


class AgentSummary(BaseModel):
    """A roster-list view of a registered agent: enough to display and pick an
    agent (including online status), but without its full agent_card or
    metadata."""

    provider_key: str
    name: str
    description: str = ""
    skills: list[dict[str, Any]] = Field(default_factory=list)
    joined_at: datetime
    last_seen_at: datetime
    online: bool = False
    provider_name: str | None = None


class AgentRecord(BaseModel):
    """The full stored record for a single registered agent, including its
    agent_card and metadata; returned by looking up one agent by id."""

    provider_key: str
    name: str
    agent_card: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    joined_at: datetime
    last_seen_at: datetime


class RunRecord(BaseModel):
    """A run's public-facing record: the same run as stored, minus internal
    storage columns like its thread messages or run events."""

    run_id: str
    thread_id: str
    provider_key: str
    agent_name: str
    protocol: str
    status: str
    input_json: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_activity_at: datetime | None = None
