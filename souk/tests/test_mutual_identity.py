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


def test_souk_can_check_a_signature_a_provider_made_over_bytes_souk_never_defined():
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


def test_a_provider_can_check_a_signature_souk_made(settings: CoreSettings):
    souk = _souk_with_identity(settings)
    payload = b"souk-auth:souk:nonce_p:nonce_s"

    assert verify_signature(souk.identity_public_key, souk.sign(payload), payload)


def test_two_souks_are_two_identities(settings: CoreSettings):
    one, other = _souk_with_identity(settings), _souk_with_identity(settings)
    payload = b"souk-auth:souk:nonce_p:nonce_s"

    assert one.identity_public_key != other.identity_public_key
    assert not verify_signature(one.identity_public_key, other.sign(payload), payload)


def test_the_same_key_is_the_same_souk_across_restarts(settings: CoreSettings):
    key = SoukIdentity.generate_hex()
    base = dict(
        database_url=settings.database_url,
        token_signing_secret=settings.token_signing_secret,
        identity_private_key=key,
    )

    before = Souk(CoreSettings(**base))
    after = Souk(CoreSettings(**base))

    assert before.identity_public_key == after.identity_public_key


def test_a_souk_without_a_key_says_so_rather_than_inventing_one(settings: CoreSettings):
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
    with pytest.raises(ValueError, match="identity_private_key"):
        Souk(
            CoreSettings(
                database_url=settings.database_url,
                token_signing_secret=settings.token_signing_secret,
                identity_private_key=bad,
            )
        )


def test_each_side_accepts_what_the_other_signs(settings: CoreSettings):
    from souk_provider_sdk import verify_signature as sdk_verify

    provider = ProviderIdentity.generate()
    souk = _souk_with_identity(settings)
    nonce_p, nonce_s = b"nonce-from-provider", b"nonce-from-souk"

    souk_proof = souk.sign(b"souk-auth:souk:" + nonce_p + b":" + nonce_s)
    assert sdk_verify(
        souk.identity_public_key, souk_proof, b"souk-auth:souk:" + nonce_p + b":" + nonce_s
    )

    provider_proof = provider.sign(b"souk-auth:provider:" + nonce_p + b":" + nonce_s)
    assert verify_signature(
        provider.public_key, provider_proof, b"souk-auth:provider:" + nonce_p + b":" + nonce_s
    )


def test_both_verifiers_reject_the_same_things(settings: CoreSettings):
    from souk_provider_sdk import verify_signature as sdk_verify

    identity = ProviderIdentity.generate()
    signature = identity.sign(b"nonce-1")
    other = ProviderIdentity.generate()

    cases = [
        (identity.public_key, signature, b"nonce-2"),
        (other.public_key, signature, b"nonce-1"),
        ("not hex", signature, b"nonce-1"),
        (identity.public_key, "not hex", b"nonce-1"),
        (identity.public_key, "", b"nonce-1"),
    ]
    for public_key, sig, payload in cases:
        assert verify_signature(public_key, sig, payload) is False
        assert sdk_verify(public_key, sig, payload) is False


def test_a_provider_pinning_one_souk_rejects_another(settings: CoreSettings):
    from souk_provider_sdk import verify_signature as sdk_verify

    pinned, impostor = _souk_with_identity(settings), _souk_with_identity(settings)
    challenge = b"souk-auth:souk:nonce_p:nonce_s"

    assert sdk_verify(pinned.identity_public_key, pinned.sign(challenge), challenge)
    assert not sdk_verify(pinned.identity_public_key, impostor.sign(challenge), challenge)


async def _register(souk: Souk, identity: ProviderIdentity, name: str) -> None:
    signature, timestamp = identity.sign_registration([name])
    await souk.register_agents(identity.public_key, signature, timestamp, [{"name": name}])


def _link(souk: Souk, identity: ProviderIdentity, **kwargs):
    from souk_provider_sdk import InProcessLink, ProviderRuntime

    return InProcessLink(souk, ProviderRuntime(identity, object()), **kwargs)


async def test_attach_answers_and_the_in_process_link_verifies_it(settings: CoreSettings):
    from souk_provider_sdk import souk_connect_payload
    from souk_provider_sdk import verify_signature as sdk_verify

    souk = _souk_with_identity(settings)
    try:
        identity = ProviderIdentity.generate()
        await _register(souk, identity, "mutual")

        challenge = souk.issue_connect_challenge()
        proof = identity.sign_connect(souk.identity_public_key, challenge, "pn", ["mutual"])
        answer = await souk.attach_provider(
            _link(souk, identity), ["mutual"], challenge=challenge, provider_nonce="pn", proof=proof
        )

        assert answer is not None
        assert sdk_verify(souk.identity_public_key, answer, souk_connect_payload(challenge, "pn"))
    finally:
        await souk.aclose()


async def test_a_pinning_link_refuses_the_wrong_souk(settings: CoreSettings):
    from souk_provider_sdk import WrongSouk

    souk = _souk_with_identity(settings)
    try:
        identity = ProviderIdentity.generate()
        await _register(souk, identity, "wary")
        elsewhere = ProviderIdentity.generate().public_key

        with pytest.raises(WrongSouk):
            await souk.attach_provider(
                _link(souk, identity, souk_public_key=elsewhere), ["wary"]
            )
        from souk.models import AgentRef

        assert not souk.is_serving(AgentRef(provider_key=identity.public_key, name="wary"))
    finally:
        await souk.aclose()


async def test_an_identityless_souk_answers_nothing_and_only_a_pin_objects(settings: CoreSettings):
    from souk_provider_sdk import WrongSouk

    souk = Souk(settings)
    try:
        identity = ProviderIdentity.generate()
        await _register(souk, identity, "trusting")

        answer = await souk.attach_provider(_link(souk, identity), ["trusting"])
        assert answer is None
        await souk.detach_provider(identity.public_key)

        pinned = ProviderIdentity.generate().public_key
        with pytest.raises(WrongSouk):
            await souk.attach_provider(
                _link(souk, identity, souk_public_key=pinned), ["trusting"]
            )
    finally:
        await souk.aclose()


async def test_a_proof_bound_to_another_souk_is_refused(settings: CoreSettings):
    from souk.errors import InvalidRegistration

    souk = _souk_with_identity(settings)
    try:
        identity = ProviderIdentity.generate()
        await _register(souk, identity, "relayed")

        challenge = souk.issue_connect_challenge()
        the_souk_it_meant = ProviderIdentity.generate().public_key
        proof = identity.sign_connect(the_souk_it_meant, challenge, "pn", ["relayed"])

        with pytest.raises(InvalidRegistration, match="invalid connect proof"):
            await souk.attach_provider(
                _link(souk, identity),
                ["relayed"],
                challenge=challenge,
                provider_nonce="pn",
                proof=proof,
            )
    finally:
        await souk.aclose()
