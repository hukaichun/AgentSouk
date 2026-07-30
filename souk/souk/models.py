"""Pydantic schemas for souk's HTTP surface.

AG-UI's `RunAgentInput` / event payloads are kept loosely typed (dicts) here
rather than fully modeled — souk treats them mostly as opaque JSON it stores,
forwards over gRPC, and relays back out, and pinning every field would mean
chasing AG-UI's own (still-evolving) schema. Only the fields souk actually
reads (thread_id, run_id, messages) are typed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentRegistration(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    description: str = ""
    agent_card_extra: dict[str, Any] = Field(default_factory=dict)


class RegisterBatchRequest(BaseModel):
    sdk_client_id: str
    agents: list[AgentRegistration]


class AgentRosterEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    joined_at: datetime
    last_seen_at: datetime
    online: bool


class RosterResponse(BaseModel):
    agents: list[AgentRosterEntry]


class RunAgentInput(BaseModel):
    """Loosely-typed AG-UI RunAgentInput — only the fields souk reads.

    thread_id and run_id are souk-assigned ids (see souk.ids): omit
    thread_id to start a new conversation (souk returns the assigned id via
    the `X-Souk-Thread-Id` response header), or pass one souk previously
    assigned to continue it. run_id is always assigned by souk regardless
    of what the caller sends, so it isn't accepted as input at all.
    """

    model_config = ConfigDict(extra="allow")

    thread_id: str | None = None
    messages: list[dict[str, Any]] = Field(default_factory=list)
