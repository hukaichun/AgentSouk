from __future__ import annotations

import time

from souk_provider_sdk.identity import ProviderIdentity

_REGISTER_LLM = "souk-register-llm"


def llm_registration_payload(names: list[str], timestamp: int) -> bytes:
    return f"{_REGISTER_LLM}:{','.join(sorted(names))}:{timestamp}".encode()


def sign_llm_registration(
    identity: ProviderIdentity, names: list[str], timestamp: int | None = None
) -> tuple[str, int]:
    timestamp = int(time.time()) if timestamp is None else timestamp
    return identity.sign(llm_registration_payload(names, timestamp)), timestamp
