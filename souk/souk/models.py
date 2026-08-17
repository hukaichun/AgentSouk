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


class AgentSummary(BaseModel):

    provider_key: str
    name: str
    description: str = ""
    skills: list[dict[str, Any]] = Field(default_factory=list)
    joined_at: datetime
    last_seen_at: datetime
    online: bool = False
    provider_name: str | None = None


class AgentRecord(BaseModel):

    provider_key: str
    name: str
    agent_card: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    joined_at: datetime
    last_seen_at: datetime


class RunRecord(BaseModel):

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
