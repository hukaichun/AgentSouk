"""Both sides can sign, and each can check the other.

Until now this was one-directional: a provider proved who it was and souk
proved nothing back, so `verify_signature` took arbitrary bytes while nothing
on either side could produce arbitrary bytes to feed it. A provider had no
way to tell one souk from another (#45), and a transport that needed a
signature for its own payload had to reach past `ProviderIdentity` for the
raw key (#43).

These assert the round trip in both directions rather than that the methods
return hex, because hex is what a broken implementation returns too.

What is *not* here is any particular payload. Proving identity as a
connection opens is a serving act, so the bytes belong to whoever serves
souk; core supplies only the primitive. The strings below are stand-ins for
that, deliberately arbitrary.
"""

from __future__ import annotations

import pytest
from souk_provider_sdk import ProviderIdentity

from souk.config import CoreSettings
from souk.core import Souk
from souk.identity import SoukIdentity, verify_signature


def _souk_with_identity(settings: CoreSettings) -> Souk:
    return Souk(
        CoreSettings(
            database_url=settings.database_url,
            token_signing_secret=settings.token_signing_secret,
            identity_private_key=SoukIdentity.generate_hex(),
        )
    )


# ---- provider → souk


def test_souk_can_check_a_signature_a_provider_made_over_bytes_souk_never_defined():
    """#43. The payload is the caller's — a gateway proving a socket belongs
    to this identity — and souk's verifier takes it without knowing what it
    means."""
    identity = ProviderIdentity.generate()
    payload = b"souk-provider-connect:whatever-the-gateway-decided:1755300000"

    assert verify_signature(identity.public_key, identity.sign(payload), payload)


def test_a_provider_signature_does_not_verify_for_a_different_payload():
    identity = ProviderIdentity.generate()

    signature = identity.sign(b"one thing")

    assert not verify_signature(identity.public_key, signature, b"another thing")


def test_a_provider_signature_does_not_verify_under_another_key():
    mine, theirs = ProviderIdentity.generate(), ProviderIdentity.generate()
    payload = b"anything"

    assert not verify_signature(theirs.public_key, mine.sign(payload), payload)


# ---- souk → provider


def test_a_provider_can_check_a_signature_souk_made(settings: CoreSettings):
    """#45, the half that did not exist. The provider side holds only souk's
    public key, and `verify_signature` is what a gateway hands it — so this
    is checked with the same function a provider would use."""
    souk = _souk_with_identity(settings)
    payload = b"souk-auth:souk:nonce_p:nonce_s"

    assert verify_signature(souk.identity_public_key, souk.sign(payload), payload)


def test_two_souks_are_two_identities(settings: CoreSettings):
    """The whole point: a provider that pinned one must not accept the other.
    Without this, "the same souk as last time" is not a checkable claim."""
    one, other = _souk_with_identity(settings), _souk_with_identity(settings)
    payload = b"souk-auth:souk:nonce_p:nonce_s"

    assert one.identity_public_key != other.identity_public_key
    assert not verify_signature(one.identity_public_key, other.sign(payload), payload)


def test_the_same_key_is_the_same_souk_across_restarts(settings: CoreSettings):
    """Configured rather than generated, so a restart — or a second replica —
    presents the identity a provider already pinned. A souk that minted its
    own would fail every pin on every restart."""
    key = SoukIdentity.generate_hex()
    base = dict(
        database_url=settings.database_url,
        token_signing_secret=settings.token_signing_secret,
        identity_private_key=key,
    )

    before = Souk(CoreSettings(**base))
    after = Souk(CoreSettings(**base))

    assert before.identity_public_key == after.identity_public_key


# ---- unconfigured, and misconfigured


def test_a_souk_without_a_key_says_so_rather_than_inventing_one(settings: CoreSettings):
    """Absent is not generated. An ephemeral identity would change on every
    restart and fail every provider's pin, which teaches people to click
    through the warning — worse than having no identity at all."""
    souk = Souk(settings)

    assert souk.identity_public_key is None
    with pytest.raises(RuntimeError, match="no identity"):
        souk.sign(b"anything")


@pytest.mark.parametrize(
    "bad, why",
    [
        ("nothex!!", "not valid hex"),
        ("abcd", "too short"),
        ("ab" * 64, "too long"),
    ],
)
def test_a_malformed_key_fails_at_construction(settings: CoreSettings, bad: str, why: str):
    """Not at the first handshake. A souk that starts and then cannot prove
    itself has already told its operators it is healthy."""
    with pytest.raises(ValueError, match="identity_private_key"):
        Souk(
            CoreSettings(
                database_url=settings.database_url,
                token_signing_secret=settings.token_signing_secret,
                identity_private_key=bad,
            )
        )
