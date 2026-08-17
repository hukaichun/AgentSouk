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


# ---- The two verifiers are two implementations, and they have to agree


def test_each_side_accepts_what_the_other_signs(settings: CoreSettings):
    """The handshake in both directions, with each side using its *own*
    verifier — which is the point.

    `souk.identity.verify_signature` and `souk_provider_sdk.verify_signature`
    are separate implementations on purpose: this package cannot import souk,
    and a copy derived from souk would agree with souk by construction and
    therefore check nothing. Same device as the signing payloads. This is
    where a drift between them fails, at merge, rather than at a handshake.
    """
    from souk_provider_sdk import verify_signature as sdk_verify

    provider = ProviderIdentity.generate()
    souk = _souk_with_identity(settings)
    nonce_p, nonce_s = b"nonce-from-provider", b"nonce-from-souk"

    # souk proves itself; the provider checks with the SDK's verifier.
    souk_proof = souk.sign(b"souk-auth:souk:" + nonce_p + b":" + nonce_s)
    assert sdk_verify(
        souk.identity_public_key, souk_proof, b"souk-auth:souk:" + nonce_p + b":" + nonce_s
    )

    # The provider proves itself; souk checks with its own.
    provider_proof = provider.sign(b"souk-auth:provider:" + nonce_p + b":" + nonce_s)
    assert verify_signature(
        provider.public_key, provider_proof, b"souk-auth:provider:" + nonce_p + b":" + nonce_s
    )


def test_both_verifiers_reject_the_same_things(settings: CoreSettings):
    """Agreeing on what is valid is half of it; agreeing on what is not is
    the half an attacker cares about."""
    from souk_provider_sdk import verify_signature as sdk_verify

    identity = ProviderIdentity.generate()
    signature = identity.sign(b"nonce-1")
    other = ProviderIdentity.generate()

    cases = [
        (identity.public_key, signature, b"nonce-2"),      # wrong payload
        (other.public_key, signature, b"nonce-1"),         # wrong key
        ("not hex", signature, b"nonce-1"),                # malformed key
        (identity.public_key, "not hex", b"nonce-1"),      # malformed signature
        (identity.public_key, "", b"nonce-1"),             # empty signature
    ]
    for public_key, sig, payload in cases:
        assert verify_signature(public_key, sig, payload) is False
        assert sdk_verify(public_key, sig, payload) is False


def test_a_provider_pinning_one_souk_rejects_another(settings: CoreSettings):
    """What #45 is for, checked from the side that has to act on it: the
    provider holds a public key it pinned, and the SDK's verifier is what
    turns that into a decision."""
    from souk_provider_sdk import verify_signature as sdk_verify

    pinned, impostor = _souk_with_identity(settings), _souk_with_identity(settings)
    challenge = b"souk-auth:souk:nonce_p:nonce_s"

    assert sdk_verify(pinned.identity_public_key, pinned.sign(challenge), challenge)
    assert not sdk_verify(pinned.identity_public_key, impostor.sign(challenge), challenge)
