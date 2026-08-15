"""Pydantic schemas for souk's HTTP surface.

`RunAgentInput` itself is deliberately *not* defined here — the inbound
`/agui/{name}` request body uses `ag_ui.core.RunAgentInput`, the real
AG-UI schema, directly (see api_agui.py). A separate, souk-flavored
reimplementation of the same model used to live here, which meant two
different types with the same name, only one of which was the real
protocol — and the souk-only one was missing `tools`/`state`/`context`
entirely, fields a real caller may legitimately want to send. There is
nothing left to duplicate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AgentRegistration(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    description: str = ""
    agent_card_extra: dict[str, Any] = Field(default_factory=dict)
    # souk-internal, not exposed via the public A2A Agent Card — see
    # agents.metadata in souk/schema.py.
    metadata: dict[str, Any] = Field(default_factory=dict)


class RegisterBatchRequest(BaseModel):
    agents: list[AgentRegistration]
    # Optional storefront label for this public_key, shown when
    # souk-directory groups agents by provider — see souk/schema.py's
    # providers table. Purely descriptive (like AgentRegistration.
    # description below), so — like description — it's not part of what
    # registration_signing_payload covers; only which names are being
    # claimed needs to be tamper-proof.
    provider_name: str | None = Field(default=None, max_length=128)
    # Ed25519 public key (hex) this provider's identity is backed by, and
    # a signature (hex) over souk.identity.registration_signing_payload —
    # proves possession of the matching private key. See
    # souk_agent_sdk.identity for how a provider generates/persists one.
    # First registration of a name binds it to this key; later attempts
    # to register the same name with a different key are rejected (see
    # repo.register_agents) — this is the whole of souk's provider
    # identity model: no signup flow, the keypair *is* the identity.
    public_key: str
    signature: str
    # Unix timestamp (seconds) included in what was signed — souk rejects
    # anything outside souk.identity.SIGNATURE_FRESHNESS_WINDOW_SECONDS of
    # its own clock, so a captured signed request can't be replayed
    # indefinitely to keep minting fresh session tokens.
    timestamp: int


class AgentRosterEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent_id: str
    name: str
    description: str = ""
    skills: list[dict[str, Any]] = Field(default_factory=list)
    joined_at: datetime
    last_seen_at: datetime
    online: bool
    # Whoever holds this key owns this agent — see AgentRosterEntry's
    # module docstring / repo.register_agents. provider_name is the
    # optional storefront label for that key (souk.schema's providers table),
    # None if that public_key never set one.
    public_key: str
    provider_name: str | None = None


class RosterResponse(BaseModel):
    agents: list[AgentRosterEntry]


class RegisterBatchResponse(RosterResponse):
    # Bearer token required on every subsequent PollForWork/AgentSession
    # gRPC call (see souk.identity) — valid for
    # souk.identity.SESSION_TOKEN_TTL_SECONDS, re-issued on every
    # /agents/register call (souk_agent_sdk re-registers on each
    # run_forever() (re)connect, so an expired token is naturally
    # refreshed rather than needing its own renewal endpoint).
    session_token: str
    # {name: agent_id} for this batch — lets the caller (souk_agent_sdk)
    # learn the souk-assigned ids for the agents it just registered, since
    # `name` alone is no longer a unique routing key (see souk/schema.py).
    agent_ids: dict[str, str]


class CreateThreadRequest(BaseModel):
    # agent_id comes from the URL path (POST /threads/id/{agent_id} or
    # POST /threads/{name}, mirroring /agui's own id-vs-name routes) —
    # this body is just the optional extras.
    metadata: dict[str, Any] = Field(default_factory=dict)


class CreateThreadResponse(BaseModel):
    thread_id: str


class LivenessResponse(BaseModel):
    """`/healthz`. Nothing about souk's dependencies belongs here — the
    response existing is the answer."""

    status: str


class HealthResponse(BaseModel):
    """`/readyz`. Facts, so an operator reading a 503 does not have to go
    and find out which of them was false. Carries no connection string and
    no driver message — see souk.core.Health, and note that this endpoint is
    unauthenticated because a probe cannot hold a credential.
    """

    ready: bool
    database: bool
    # The exception's type name, never its message.
    database_error: str | None = None
    schema_revision: str | None = None
    expected_schema_revision: str
    background_running: bool
