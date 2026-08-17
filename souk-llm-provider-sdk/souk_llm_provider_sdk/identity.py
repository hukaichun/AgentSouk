from __future__ import annotations

import time

from souk_provider_sdk.identity import ProviderIdentity

_REGISTER_LLM = "souk-register-llm"


def llm_registration_payload(names: list[str], timestamp: int) -> bytes:
    """Builds the bytes to sign for registering as an LLM provider under `names`.

    Names are sorted before joining, so registration order does not affect the
    payload or its signature.
    """
    return f"{_REGISTER_LLM}:{','.join(sorted(names))}:{timestamp}".encode()


def sign_llm_registration(
    identity: ProviderIdentity, names: list[str], timestamp: int | None = None
) -> tuple[str, int]:
    """Signs a registration payload for `names`, defaulting the timestamp to now.

    Returns the `(signature, timestamp)` pair actually signed over, so callers
    can send both to the verifier.
    """
    timestamp = int(time.time()) if timestamp is None else timestamp
    return identity.sign(llm_registration_payload(names, timestamp)), timestamp
