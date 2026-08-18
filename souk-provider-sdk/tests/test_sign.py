from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import pytest

from souk_provider_sdk import ProviderIdentity, verify_signature


def _verify(identity: ProviderIdentity, signature_hex: str, payload: bytes) -> bool:
    public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(identity.public_key))
    try:
        public_key.verify(bytes.fromhex(signature_hex), payload)
        return True
    except InvalidSignature:
        return False


def test_it_signs_bytes_nobody_here_chose():
    identity = ProviderIdentity.generate()
    payload = b"souk-provider-connect:abc:translator,summarizer:1755300000"

    assert _verify(identity, identity.sign(payload), payload)


def test_the_signature_is_over_the_payload_and_not_something_else():
    identity = ProviderIdentity.generate()

    assert not _verify(identity, identity.sign(b"one thing"), b"another thing")


def test_empty_and_binary_payloads_are_signable():
    identity = ProviderIdentity.generate()

    for payload in (b"", bytes(range(256)), b"\x00\xff\x00"):
        assert _verify(identity, identity.sign(payload), payload)


def test_nothing_is_refused_for_looking_like_souks_own_payloads():
    identity = ProviderIdentity.generate()
    payload = b"souk-register:translator:1755300000"

    assert _verify(identity, identity.sign(payload), payload)


def test_two_identities_do_not_verify_for_each_other():
    mine, theirs = ProviderIdentity.generate(), ProviderIdentity.generate()
    payload = b"anything"

    assert not _verify(theirs, mine.sign(payload), payload)


def test_the_named_signers_still_agree_with_the_general_one():
    from souk_provider_sdk import registration_payload

    identity = ProviderIdentity.generate()
    timestamp = 1755300000

    named, _ = identity.sign_registration(["translator"], timestamp)
    general = identity.sign(registration_payload(["translator"], timestamp))

    assert named == general


def test_it_accepts_what_this_package_signs():
    identity = ProviderIdentity.generate()
    payload = b"souk-auth:souk:nonce_p:nonce_s"

    assert verify_signature(identity.public_key, identity.sign(payload), payload)


def test_it_rejects_a_signature_over_different_bytes():
    identity = ProviderIdentity.generate()

    assert not verify_signature(identity.public_key, identity.sign(b"nonce-1"), b"nonce-2")


def test_it_rejects_a_signature_from_another_key():
    mine, theirs = ProviderIdentity.generate(), ProviderIdentity.generate()
    payload = b"anything"

    assert not verify_signature(theirs.public_key, mine.sign(payload), payload)


@pytest.mark.parametrize(
    "public_key, signature",
    [
        ("not hex", "aa" * 64),
        ("ab" * 32, "not hex"),
        ("", "aa" * 64),
        ("ab" * 32, ""),
        ("ab" * 8, "aa" * 64),
    ],
)
def test_malformed_input_is_false_and_not_an_exception(public_key: str, signature: str):
    assert verify_signature(public_key, signature, b"payload") is False
