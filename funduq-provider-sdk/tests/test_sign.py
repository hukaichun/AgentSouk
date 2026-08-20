from __future__ import annotations

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

import pytest

from funduq_provider_sdk import ProviderIdentity, verify_signature


def _verify(identity: ProviderIdentity, signature_hex: str, payload: bytes) -> bool:
    public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(identity.public_key))
    try:
        public_key.verify(bytes.fromhex(signature_hex), payload)
        return True
    except InvalidSignature:
        return False


def test_it_signs_bytes_nobody_here_chose():
    identity = ProviderIdentity.generate()
    payload = b"any bytes at all \x00\xff -- the SDK does not inspect them"

    assert _verify(identity, identity.sign(payload), payload)


def test_the_signature_is_over_the_payload_and_not_something_else():
    identity = ProviderIdentity.generate()

    assert not _verify(identity, identity.sign(b"one thing"), b"another thing")


def test_empty_and_binary_payloads_are_signable():
    identity = ProviderIdentity.generate()

    for payload in (b"", bytes(range(256)), b"\x00\xff\x00"):
        assert _verify(identity, identity.sign(payload), payload)


def test_nothing_is_refused_for_looking_like_funduqs_own_payloads():
    identity = ProviderIdentity.generate()
    payload = b"funduq-register:translator:1755300000"

    assert _verify(identity, identity.sign(payload), payload)


def test_two_identities_do_not_verify_for_each_other():
    mine, theirs = ProviderIdentity.generate(), ProviderIdentity.generate()
    payload = b"anything"

    assert not _verify(theirs, mine.sign(payload), payload)


def test_the_named_signers_still_agree_with_the_general_one():
    from funduq_provider_sdk import registration_payload

    identity = ProviderIdentity.generate()
    timestamp = 1755300000

    named, _ = identity.sign_registration(["translator"], timestamp)
    general = identity.sign(registration_payload(["translator"], timestamp))

    assert named == general


def test_it_accepts_what_this_package_signs():
    identity = ProviderIdentity.generate()
    payload = b"funduq-auth:funduq:nonce_p:nonce_s"

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


def test_a_link_open_round_trip_and_neither_proof_reflects_as_the_other():
    from funduq_provider_sdk import new_nonce, provider_connect_payload, funduq_connect_payload

    provider = ProviderIdentity.generate()
    funduq_key = ProviderIdentity.generate()
    funduq_nonce, provider_nonce = new_nonce(), new_nonce()
    assert funduq_nonce != provider_nonce and len(funduq_nonce) == 32

    proof = provider.sign_connect(funduq_key.public_key, funduq_nonce, provider_nonce, ["b", "a"])
    payload = provider_connect_payload(funduq_key.public_key, funduq_nonce, provider_nonce, ["a", "b"])
    assert verify_signature(provider.public_key, proof, payload)

    answer = funduq_key.sign(funduq_connect_payload(funduq_nonce, provider_nonce))
    assert verify_signature(
        funduq_key.public_key, answer, funduq_connect_payload(funduq_nonce, provider_nonce)
    )

    assert not verify_signature(
        provider.public_key, proof, funduq_connect_payload(funduq_nonce, provider_nonce)
    )
    assert not verify_signature(
        provider.public_key,
        proof,
        provider_connect_payload(funduq_key.public_key, funduq_nonce, provider_nonce, ["a", "b", "c"]),
    )
    assert not verify_signature(
        provider.public_key,
        proof,
        provider_connect_payload(funduq_key.public_key, new_nonce(), provider_nonce, ["a", "b"]),
    )
    assert not verify_signature(
        provider.public_key,
        proof,
        provider_connect_payload(
            ProviderIdentity.generate().public_key, funduq_nonce, provider_nonce, ["a", "b"]
        ),
    ), "a proof bound to one funduq must not verify as a proof for another"
