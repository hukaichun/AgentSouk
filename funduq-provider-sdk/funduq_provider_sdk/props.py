from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class VerifiedActor(BaseModel):
    """One hop of a verified actor chain, resolved against funduq's roster: the hop's signing key, and the agent name registered under it (None if unregistered)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    public_key: str = Field(alias="publicKey")
    agent_name: str | None = Field(default=None, alias="agentName")


class CallerProps(BaseModel):
    """funduq's `forwardedProps.caller` entry, as this side validates it: the verified caller identity attached to a dispatched run.

    The independent twin of `funduq.props.CallerProps` — neither package
    imports the other; the delivered-run frame in
    `docs/contract-vectors.json` pins the two byte-for-byte. Validate with
    this model rather than restating it: `chain` is None whenever funduq
    verified a subject but has no raw hop JWTs to pass on, and a restated
    copy that got that nullability wrong once dropped verified identities
    silently — a run simply ran anonymous.
    """

    model_config = ConfigDict(frozen=True)

    subject: Any
    actors: list[VerifiedActor] = []
    chain: list[str] | None = None


class KyokForwardedProps(BaseModel):
    """funduq's `forwardedProps.kyok` entry: the grant a KYOK-bound run's agent presents when calling for completions.

    The independent twin of `funduq.kyok.KyokForwardedProps`, pinned by the
    same delivered-run frame. The token is opaque — carry it, sign over it
    (`kyok_call_payload`), never parse it.
    """

    model_config = ConfigDict(frozen=True)

    token: str
