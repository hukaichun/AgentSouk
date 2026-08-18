from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

from openai.types.chat import ChatCompletionChunk, CompletionCreateParams
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from souk.models import AgentRef, LlmRef

KYOK_TOKEN_TTL_SECONDS = 3600


@dataclass
class KyokToken:
    run_id: str
    agent: AgentRef


def issue_kyok_token(run_id: str, agent: AgentRef, signing_secret: str) -> str:
    """Build a `body.signature` token binding `run_id` and `agent`, expiring after `KYOK_TOKEN_TTL_SECONDS`.

    `body` is a base64url JSON object with exactly `runId`, `providerKey`, `agentName`,
    and `exp`; `signature` is an HMAC-SHA256 of `body` keyed by `signing_secret`.
    """
    body = base64.urlsafe_b64encode(
        json.dumps(
            {
                "runId": run_id,
                "providerKey": agent.provider_key,
                "agentName": agent.name,
                "exp": int(time.time()) + KYOK_TOKEN_TTL_SECONDS,
            }
        ).encode()
    ).decode()
    signature = hmac.new(signing_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def verify_kyok_token(token: str, signing_secret: str) -> KyokToken | None:
    """Return the decoded (run_id, agent) if `token` is well-formed, correctly signed, and unexpired.

    Returns None for a token that doesn't split into `body.signature`, fails signature
    verification, has expired (`exp` in the past), isn't valid JSON/base64, or is missing
    any of `runId`/`providerKey`/`agentName` as strings.
    """
    try:
        body, signature = token.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(signing_secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode()))
    except (ValueError, UnicodeDecodeError):
        return None
    if payload.get("exp", 0) < time.time():
        return None
    run_id = payload.get("runId")
    provider_key = payload.get("providerKey")
    agent_name = payload.get("agentName")
    if not all(isinstance(v, str) for v in (run_id, provider_key, agent_name)):
        return None
    return KyokToken(
        run_id=run_id,
        agent=AgentRef(provider_key=provider_key, name=agent_name),
    )


class KyokOptIn(BaseModel):
    """A run's opt-in into KYOK: which LLM offering to bind to, plus opaque caller context."""

    model_config = ConfigDict(frozen=True, extra="allow", populate_by_name=True)

    llm_provider: LlmRef | None = Field(default=None, alias="llmProvider")
    context: Any = None


def parse_kyok_opt_in(metadata: dict) -> KyokOptIn | None:
    """Parse `metadata["kyok"]` into a `KyokOptIn`, or None if absent or malformed (never raises)."""
    raw = metadata.get("kyok")
    if not isinstance(raw, dict):
        return None
    target = raw.get("llmProvider")
    if isinstance(target, dict):
        raw = {**raw, "llmProvider": {"provider_key": target.get("providerKey"), "name": target.get("name")}}
    try:
        return KyokOptIn.model_validate(raw)
    except ValidationError:
        return None


def strip_kyok_context(metadata: dict) -> dict:
    """Return a copy of `metadata` with `kyok.context` removed, leaving everything else untouched."""
    kyok = metadata.get("kyok")
    if isinstance(kyok, dict) and "context" in kyok:
        return {**metadata, "kyok": {k: v for k, v in kyok.items() if k != "context"}}
    return metadata


class KyokForwardedProps(BaseModel):

    model_config = ConfigDict(frozen=True)

    token: str


def kyok_forwarded_props(run_id: str, agent: AgentRef, signing_secret: str) -> dict[str, Any]:
    """Issue a KYOK token for the run and wrap it as the dict to send under the `kyok` forwarded prop."""
    return KyokForwardedProps(
        token=issue_kyok_token(run_id, agent, signing_secret)
    ).model_dump()


def read_kyok_forwarded_props(forwarded_props: Any) -> KyokForwardedProps | None:
    """Extract the `kyok` entry from `forwarded_props`, or None if missing, not a dict, or invalid."""
    if not isinstance(forwarded_props, dict):
        return None
    raw = forwarded_props.get("kyok")
    if raw is None:
        return None
    try:
        return KyokForwardedProps.model_validate(raw)
    except ValidationError:
        return None


@dataclass(frozen=True)
class KyokBinding:

    llm_provider: LlmRef
    context: Any = None
    actor_chain: list[str] | None = None


@dataclass(frozen=True)
class CompletionRequest:

    run_id: str
    agent: AgentRef
    body: CompletionCreateParams
    llm_name: str = ""
    context: Any = None
    actor_chain: list[str] | None = None


class ConnectedLLMProvider(Protocol):

    public_key: str

    def complete(self, request: CompletionRequest) -> AsyncIterator[ChatCompletionChunk]: ...


@dataclass(frozen=True)
class LlmProviderQuality:
    """Per-LLM-provider counters of what souk observed while relaying completions — the mirror of the broker's `ProviderQuality`.

    `completions` streamed to the end; `refused` ended in a structured
    refusal; `failed` died with anything else. souk counts what it saw and
    judges nothing.
    """

    completions: int = 0
    refused: int = 0
    failed: int = 0


class KyokRelay:
    """Tracks which LLM offering each in-flight run is bound to, and which links serve each offering."""

    def __init__(self) -> None:
        self._bindings: dict[str, KyokBinding] = {}
        self._links: dict[LlmRef, ConnectedLLMProvider] = {}
        self._quality: dict[str, dict[str, int]] = {}


    def bind_run(self, run_id: str, binding: KyokBinding) -> None:
        self._bindings[run_id] = binding

    def binding_for(self, run_id: str) -> KyokBinding | None:
        return self._bindings.get(run_id)

    def inherit(self, parent_run_id: str, child_run_id: str, actor_chain: list[str] | None) -> bool:
        """Bind `child_run_id` to the parent run's offering and context, with its own `actor_chain`.

        Returns False (and binds nothing) if the parent run has no binding.
        """
        parent = self._bindings.get(parent_run_id)
        if parent is None:
            return False
        self._bindings[child_run_id] = KyokBinding(
            llm_provider=parent.llm_provider,
            context=parent.context,
            actor_chain=actor_chain,
        )
        return True

    def discard(self, run_id: str) -> None:
        self._bindings.pop(run_id, None)


    def attach(self, mapping: dict[LlmRef, ConnectedLLMProvider]) -> None:
        self._links.update(mapping)

    def withdraw(self, refs: list[LlmRef]) -> None:
        for ref in refs:
            self._links.pop(ref, None)

    def serving(self, ref: LlmRef) -> ConnectedLLMProvider | None:
        return self._links.get(ref)

    def served_by(self, public_key: str) -> list[LlmRef]:
        return [ref for ref in self._links if ref.provider_key == public_key]

    def bound_runs(self, ref: LlmRef) -> int:
        return sum(1 for b in self._bindings.values() if b.llm_provider == ref)

    def note_outcome(self, public_key: str, outcome: str) -> None:
        counters = self._quality.setdefault(
            public_key, {"completions": 0, "refused": 0, "failed": 0}
        )
        counters[outcome] += 1

    def quality(self) -> dict[str, LlmProviderQuality]:
        return {key: LlmProviderQuality(**counters) for key, counters in self._quality.items()}
