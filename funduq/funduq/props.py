from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from funduq.kyok import kyok_forwarded_props
from funduq.models import AgentRef


INTERJECTION_EXTENSION_URI = "https://github.com/hukaichun/funduq/ext/interjection/v1"
"""The A2A extension under which a caller declares an *interjection*: a run
that asks to join another run's turn already in flight. This is intent, not
state — `parentRunId` (AG-UI's own field, relayed untouched) says "this
follows that, next turn"; `addressedRunId` says "this wants *into* that turn
now". The two are different verbs and the caller chooses one; liveness of
the target is never used to guess intent. A2A v1.0 has no carrier for
unprompted speech into a working task (its only mid-task verb is cancel), so
this rides A2A's extension convention: the caller puts the target's id in
message metadata under `f"{INTERJECTION_EXTENSION_URI}/addressedRunId"`.
funduq relays it to the agent as `forwardedProps.addressedRunId` and holds
no opinion about the target's state — the agent running it judges whether
there is still a turn to join, and an ask that comes too late degrades to an
ordinary next turn. Yields to whatever carrier A2A ships for this."""

ADDRESSED_RUN_METADATA_KEY = f"{INTERJECTION_EXTENSION_URI}/addressedRunId"


RESERVED_METADATA_KEYS = frozenset(
    {"verifiedActorChain", "interrupts", "failureReason", "funduq"}
)
"""Metadata keys funduq itself writes into a run's record (plus "funduq", held in
reserve). A caller-supplied value under any of these is stripped at the doors
before anything reads or stores the metadata — otherwise a caller could plant
a forged `verifiedActorChain` (or a fake failure reason) that would sit in the
record wearing funduq's handwriting. The strip happens in one place,
`protocols.agui.verify_caller`, because both doors funnel caller metadata
through it."""


class VerifiedActor(BaseModel):
    """One hop of a verified actor chain, resolved against the roster: the hop's signing key, and the agent name registered under it (None if unregistered)."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    public_key: str = Field(alias="publicKey")
    agent_name: str | None = Field(default=None, alias="agentName")


class CallerProps(BaseModel):
    """funduq's `forwardedProps.caller` entry: the verified caller identity attached to a dispatched run.

    AG-UI leaves `forwardedProps` free-form for the caller; `caller` and
    `kyok` (`funduq.kyok.KyokForwardedProps`) are the two keys funduq itself
    adds, and this model is their declaration on funduq's side.
    `funduq_provider_sdk.props.CallerProps` is the independent twin a
    provider validates with — neither package imports the other, and the
    delivered-run frame in `docs/contract-vectors.json` pins the two
    byte-for-byte. `subject` and each hop come from
    `verify_actor_chain`; `chain` is the raw hop JWTs so the provider
    can re-verify without trusting funduq's summary.
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
    addressed_run_id: str | None = None,
) -> Any:
    """Merges funduq-added forwarded-props extras (a KYOK grant if `kyok_enabled`, verified caller
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
    if addressed_run_id is not None:
        # The caller's declared interjection intent (see
        # INTERJECTION_EXTENSION_URI). AG-UI callers write this key into
        # their own forwardedProps directly and it passes through untouched;
        # the A2A door copies it here from the extension's metadata key.
        extra["addressedRunId"] = addressed_run_id
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
