from __future__ import annotations

from souk_provider_sdk.identity import (
    ProviderIdentity,
    roster_registration_payload,
    sign_roster_registration,
)

_REGISTER_LLM = "souk-register-llm"


def llm_registration_payload(names: list[str], timestamp: int) -> bytes:
    """Builds the bytes to sign for registering as an LLM provider under `names`: `roster_registration_payload` under the LLM tag."""
    return roster_registration_payload(_REGISTER_LLM, names, timestamp)


def sign_llm_registration(
    identity: ProviderIdentity, names: list[str], timestamp: int | None = None
) -> tuple[str, int]:
    """Signs a registration payload for `names`, defaulting the timestamp to now.

    Returns the `(signature, timestamp)` pair actually signed over, so callers
    can send both to the verifier.
    """
    return sign_roster_registration(identity, _REGISTER_LLM, names, timestamp)
