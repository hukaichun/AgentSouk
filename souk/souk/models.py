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
    # souk-internal, not exposed via the public A2A Agent Card — see
    # agents.metadata in souk/db.py.
    metadata: dict[str, Any] = Field(default_factory=dict)


class RegisterBatchRequest(BaseModel):
    sdk_client_id: str
    agents: list[AgentRegistration]
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

    name: str
    joined_at: datetime
    last_seen_at: datetime
    online: bool


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
    # Free-form extension data for this run, stored on its thread_history
    # row (see souk/db.py) — not interpreted by souk itself.
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Set to explicitly continue a thread whose active run paused
    # (status='input-required', see souk/pause.py) — this is the only
    # thing that's allowed to start a new run on a thread that already
    # has one. A plain call without this flag on a paused thread is
    # treated as a duplicate/accidental re-trigger and gets back the
    # thread's current state instead of starting anything (see
    # repo.get_active_run_for_thread's docstring).
    resume: bool = False
