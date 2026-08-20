from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from souk.kyok import kyok_forwarded_props
from souk.models import AgentRef


RESERVED_METADATA_KEYS = frozenset(
    {"verifiedActorChain", "addressedRunId", "interrupts", "failureReason", "souk"}
)
"""Metadata keys souk itself writes into a run's record (plus "souk", held in
reserve). A caller-supplied value under any of these is stripped at the doors
before anything reads or stores the metadata — otherwise a caller could plant
a forged `verifiedActorChain` (or a fake failure reason) that would sit in the
record wearing souk's handwriting. The strip happens in one place,
`protocols.agui.verify_caller`, because both doors funnel caller metadata
through it."""


class VerifiedActor(BaseModel):
    """One hop of a verified actor chain, resolved against the roster: the hop's signing key, and the agent name registered under it (None if unregistered)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    public_key: str = Field(alias="publicKey")
    agent_name: str | None = Field(default=None, alias="agentName")


class CallerProps(BaseModel):
    """souk's `forwardedProps.caller` entry: the verified caller identity attached to a dispatched run.

    AG-UI leaves `forwardedProps` free-form for the caller; `caller` and
    `kyok` (`souk.kyok.KyokForwardedProps`) are the two keys souk itself
    adds, and this model is their declaration on souk's side.
    `souk_provider_sdk.props.CallerProps` is the independent twin a
    provider validates with — neither package imports the other, and the
    delivered-run frame in `docs/contract-vectors.json` pins the two
    byte-for-byte. `subject` and each hop come from
    `verify_actor_chain`; `chain` is the raw hop JWTs so the provider
    can re-verify without trusting souk's summary.
    """

    model_config = ConfigDict(frozen=True)

    subject: Any
    actors: list[VerifiedActor] = []
    chain: list[str] | None = None


def build_forwarded_props(
    signing_secret: str,
    run_id: str,
    agent: AgentRef,
    kyok_enabled: bool,
    caller_forwarded_props: Any,
    verified_subject: Any = None,
    verified_actors: list[dict] | None = None,
    actor_chain: Any = None,
) -> Any:
    """Merges souk-added forwarded-props extras (a KYOK grant if `kyok_enabled`, verified caller
    identity if present) into the caller-supplied `forwarded_props`, returning the caller's value
    unchanged if there is nothing to add.

    Both adapters build through here — a run's `caller` looks the same
    whichever protocol dispatched it, and both doors take the same
    `metadata.kyok` opt-in. A2A additionally inherits a parent's binding
    through `referenceTaskIds` when no fresh opt-in is given; an explicit
    opt-in wins over inheritance.
    """
    extra: dict[str, Any] = {}
    if kyok_enabled:
        extra["kyok"] = kyok_forwarded_props(run_id, agent, signing_secret)
    if verified_subject is not None:
        extra["caller"] = CallerProps(
            subject=verified_subject,
            actors=[VerifiedActor.model_validate(a) for a in verified_actors or []],
            chain=actor_chain,
        ).model_dump(mode="json", by_alias=True)
    if not extra:
        return caller_forwarded_props
    if isinstance(caller_forwarded_props, dict):
        return {**caller_forwarded_props, **extra}
    return extra
