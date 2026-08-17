"""`ProviderIdentity.sign` — arbitrary bytes, for payloads souk does not define.

souk's own suite checks the cross-boundary half (that `verify_signature`
accepts what this produces). It cannot be checked from here: this package
does not depend on souk and `import souk` fails in this environment, which is
the boundary working. So these check the property from the crypto's own side,
with the public key.
"""

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
    """The point of the method: a transport's own connect payload, whose
    format this package neither defines nor needs to know."""
    identity = ProviderIdentity.generate()
    payload = b"souk-provider-connect:abc:translator,summarizer:1755300000"

    assert _verify(identity, identity.sign(payload), payload)


def test_the_signature_is_over_the_payload_and_not_something_else():
    identity = ProviderIdentity.generate()

    assert not _verify(identity, identity.sign(b"one thing"), b"another thing")


def test_empty_and_binary_payloads_are_signable():
    """No framing assumptions. A nonce is bytes, not text, and a challenge
    this package never sees the format of may be either."""
    identity = ProviderIdentity.generate()

    for payload in (b"", bytes(range(256)), b"\x00\xff\x00"):
        assert _verify(identity, identity.sign(payload), payload)


def test_nothing_is_refused_for_looking_like_souks_own_payloads():
    """Deliberate. Refusing souk's prefixes protects against nothing —
    `sign_registration` is public on this same object — and would put this
    package back in the business of deciding what its holder may sign, which
    is the problem it exists to fix. Distinctness is the payload author's job.
    """
    identity = ProviderIdentity.generate()
    payload = b"souk-register:translator:1755300000"

    assert _verify(identity, identity.sign(payload), payload)


def test_two_identities_do_not_verify_for_each_other():
    mine, theirs = ProviderIdentity.generate(), ProviderIdentity.generate()
    payload = b"anything"

    assert not _verify(theirs, mine.sign(payload), payload)


def test_the_named_signers_still_agree_with_the_general_one():
    """`sign_registration` is `sign` over a payload this package defines, so
    the two must not drift apart — a second signing path that skipped the
    payload builder is how the two sides stopped agreeing before."""
    from souk_provider_sdk import registration_payload

    identity = ProviderIdentity.generate()
    timestamp = 1755300000

    named, _ = identity.sign_registration(["translator"], timestamp)
    general = identity.sign(registration_payload(["translator"], timestamp))

    assert named == general


# ---- verify_signature: the half this package was missing


def test_it_accepts_what_this_package_signs():
    identity = ProviderIdentity.generate()
    payload = b"souk-auth:souk:nonce_p:nonce_s"

    assert verify_signature(identity.public_key, identity.sign(payload), payload)


def test_it_rejects_a_signature_over_different_bytes():
    """The whole reason a challenge works: yesterday's answer is a signature
    over yesterday's nonce."""
    identity = ProviderIdentity.generate()

    assert not verify_signature(identity.public_key, identity.sign(b"nonce-1"), b"nonce-2")


def test_it_rejects_a_signature_from_another_key():
    """What tells one souk from another. A relay can pass frames through and
    still cannot produce this."""
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
        ("ab" * 8, "aa" * 64),   # right shape, wrong length
    ],
)
def test_malformed_input_is_false_and_not_an_exception(public_key: str, signature: str):
    """Everything here arrived over a wire. A caller asking "is this valid?"
    gets an answer, not a traceback to guard against at every call site."""
    assert verify_signature(public_key, signature, b"payload") is False
