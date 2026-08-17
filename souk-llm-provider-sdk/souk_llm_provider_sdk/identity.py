"""What an LLM provider's registration signs — this package's own
statement of it.

Deliberately not imported from souk, for the reason souk-provider-sdk's
identity module states at length: a copy derived from souk agrees with
souk by construction and therefore checks nothing. souk's suite compares
this builder against souk.identity.llm_registration_signing_payload, so
the two moving apart fails at merge time instead of at the first real
registration.

The keypair itself is souk_provider_sdk.ProviderIdentity, re-exported by
this package's __init__ — identity is identity, and an LLM provider that
is also an agent provider signs both rosters with the one key it is.
"""

from __future__ import annotations

import time

from souk_provider_sdk.identity import ProviderIdentity

_REGISTER_LLM = "souk-register-llm"


def llm_registration_payload(names: list[str], timestamp: int) -> bytes:
    """Which model offerings are being declared, and when. An offering is
    `(provider_key, name)` — names are free across identities, so this
    claims no ownership of a word, exactly like agent registration. The
    identity is the public key presented alongside; the operation prefix
    keeps a signature captured for this roster from being presentable to
    the agent roster, however unlikely."""
    return f"{_REGISTER_LLM}:{','.join(sorted(names))}:{timestamp}".encode()


def sign_llm_registration(
    identity: ProviderIdentity, names: list[str], timestamp: int | None = None
) -> tuple[str, int]:
    """Returns `(signature_hex, timestamp)` — both, because souk verifies
    the signature *over* the timestamp and would otherwise be handed two
    values that do not belong together."""
    timestamp = int(time.time()) if timestamp is None else timestamp
    return identity.sign(llm_registration_payload(names, timestamp)), timestamp
