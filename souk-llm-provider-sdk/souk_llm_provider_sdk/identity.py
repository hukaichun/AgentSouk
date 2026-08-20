from __future__ import annotations

import time

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


_DELETE_LLM = "souk-delete-llm"


def llm_deletion_payload(name: str, timestamp: int) -> bytes:
    """Builds the bytes to sign for deleting one offering, under its own domain tag so a registration signature can't be replayed as a deletion order.

    `souk.identity.llm_deletion_signing_payload` computes this same payload
    independently on souk's side and both must agree byte-for-byte.
    """
    return f"{_DELETE_LLM}:{name}:{timestamp}".encode()


def sign_llm_deletion(
    identity: ProviderIdentity, name: str, timestamp: int | None = None
) -> tuple[str, int]:
    """Signs a deletion payload for `name`, defaulting the timestamp to now; returns the `(signature, timestamp)` pair actually signed over."""
    timestamp = int(time.time()) if timestamp is None else timestamp
    return identity.sign(llm_deletion_payload(name, timestamp)), timestamp
