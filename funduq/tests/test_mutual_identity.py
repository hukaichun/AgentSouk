from __future__ import annotations

import pytest
from funduq_provider_sdk import ProviderIdentity

from funduq.config import CoreSettings
from funduq.core import Funduq
from funduq.identity import FunduqIdentity, verify_signature


def _funduq_with_identity(settings: CoreSettings) -> Funduq:
    return Funduq(
        CoreSettings(
            database_url=settings.database_url,
            token_signing_secret=settings.token_signing_secret,
            identity_private_key=FunduqIdentity.generate_hex(),
        )
    )


def test_funduq_can_check_a_signature_a_provider_made_over_bytes_funduq_never_defined():
    identity = ProviderIdentity.generate()
    payload = b"funduq-provider-connect:whatever-the-gateway-decided:1755300000"

    assert verify_signature(identity.public_key, identity.sign(payload), payload)


def test_a_provider_signature_does_not_verify_for_a_different_payload():
    identity = ProviderIdentity.generate()

    signature = identity.sign(b"one thing")

    assert not verify_signature(identity.public_key, signature, b"another thing")


def test_a_provider_signature_does_not_verify_under_another_key():
    mine, theirs = ProviderIdentity.generate(), ProviderIdentity.generate()
    payload = b"anything"

    assert not verify_signature(theirs.public_key, mine.sign(payload), payload)


def test_a_provider_can_check_a_signature_funduq_made(settings: CoreSettings):
    funduq = _funduq_with_identity(settings)
    payload = b"funduq-auth:funduq:nonce_p:nonce_s"

    assert verify_signature(funduq.identity_public_key, funduq.sign(payload), payload)


def test_two_funduqs_are_two_identities(settings: CoreSettings):
    one, other = _funduq_with_identity(settings), _funduq_with_identity(settings)
    payload = b"funduq-auth:funduq:nonce_p:nonce_s"

    assert one.identity_public_key != other.identity_public_key
    assert not verify_signature(one.identity_public_key, other.sign(payload), payload)


def test_the_same_key_is_the_same_funduq_across_restarts(settings: CoreSettings):
    key = FunduqIdentity.generate_hex()
    base = dict(
        database_url=settings.database_url,
        token_signing_secret=settings.token_signing_secret,
        identity_private_key=key,
    )

    before = Funduq(CoreSettings(**base))
    after = Funduq(CoreSettings(**base))

    assert before.identity_public_key == after.identity_public_key


def test_a_funduq_without_a_key_says_so_rather_than_inventing_one(settings: CoreSettings):
    funduq = Funduq(settings)

    assert funduq.identity_public_key is None
    with pytest.raises(RuntimeError, match="no identity"):
        funduq.sign(b"anything")


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
        Funduq(
            CoreSettings(
                database_url=settings.database_url,
                token_signing_secret=settings.token_signing_secret,
                identity_private_key=bad,
            )
        )


def test_each_side_accepts_what_the_other_signs(settings: CoreSettings):
    from funduq_provider_sdk import verify_signature as sdk_verify

    provider = ProviderIdentity.generate()
    funduq = _funduq_with_identity(settings)
    nonce_p, nonce_s = b"nonce-from-provider", b"nonce-from-funduq"

    funduq_proof = funduq.sign(b"funduq-auth:funduq:" + nonce_p + b":" + nonce_s)
    assert sdk_verify(
        funduq.identity_public_key, funduq_proof, b"funduq-auth:funduq:" + nonce_p + b":" + nonce_s
    )

    provider_proof = provider.sign(b"funduq-auth:provider:" + nonce_p + b":" + nonce_s)
    assert verify_signature(
        provider.public_key, provider_proof, b"funduq-auth:provider:" + nonce_p + b":" + nonce_s
    )


def test_both_verifiers_reject_the_same_things(settings: CoreSettings):
    from funduq_provider_sdk import verify_signature as sdk_verify

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


def test_a_provider_pinning_one_funduq_rejects_another(settings: CoreSettings):
    from funduq_provider_sdk import verify_signature as sdk_verify

    pinned, impostor = _funduq_with_identity(settings), _funduq_with_identity(settings)
    challenge = b"funduq-auth:funduq:nonce_p:nonce_s"

    assert sdk_verify(pinned.identity_public_key, pinned.sign(challenge), challenge)
    assert not sdk_verify(pinned.identity_public_key, impostor.sign(challenge), challenge)


async def _register(funduq: Funduq, identity: ProviderIdentity, name: str) -> None:
    signature, timestamp = identity.sign_registration([name])
    await funduq.register_agents(identity.public_key, signature, timestamp, [{"name": name}])


def _link(funduq: Funduq, identity: ProviderIdentity, **kwargs):
    from funduq_provider_sdk import InProcessLink, ProviderRuntime

    return InProcessLink(funduq, ProviderRuntime(identity, object()), **kwargs)


async def test_attach_answers_and_the_in_process_link_verifies_it(settings: CoreSettings):
    from funduq_provider_sdk import funduq_connect_payload
    from funduq_provider_sdk import verify_signature as sdk_verify

    funduq = _funduq_with_identity(settings)
    try:
        identity = ProviderIdentity.generate()
        await _register(funduq, identity, "mutual")

        challenge = funduq.issue_connect_challenge()
        proof = identity.sign_connect(funduq.identity_public_key, challenge, "pn", ["mutual"])
        answer = await funduq.attach_provider(
            _link(funduq, identity), ["mutual"], challenge=challenge, provider_nonce="pn", proof=proof
        )

        assert answer is not None
        assert sdk_verify(funduq.identity_public_key, answer, funduq_connect_payload(challenge, "pn"))
    finally:
        await funduq.aclose()


async def test_a_pinning_link_refuses_the_wrong_funduq(settings: CoreSettings):
    from funduq_provider_sdk import WrongFunduq

    funduq = _funduq_with_identity(settings)
    try:
        identity = ProviderIdentity.generate()
        await _register(funduq, identity, "wary")
        elsewhere = ProviderIdentity.generate().public_key

        with pytest.raises(WrongFunduq):
            await funduq.attach_provider(
                _link(funduq, identity, funduq_public_key=elsewhere), ["wary"]
            )
        from funduq.models import AgentRef

        assert not funduq.is_serving(AgentRef(provider_key=identity.public_key, name="wary"))
    finally:
        await funduq.aclose()


async def test_an_identityless_funduq_answers_nothing_and_only_a_pin_objects(settings: CoreSettings):
    from funduq_provider_sdk import WrongFunduq

    funduq = Funduq(settings)
    try:
        identity = ProviderIdentity.generate()
        await _register(funduq, identity, "trusting")

        answer = await funduq.attach_provider(_link(funduq, identity), ["trusting"])
        assert answer is None
        funduq.detach_all_for(identity.public_key)

        pinned = ProviderIdentity.generate().public_key
        with pytest.raises(WrongFunduq):
            await funduq.attach_provider(
                _link(funduq, identity, funduq_public_key=pinned), ["trusting"]
            )
    finally:
        await funduq.aclose()


async def test_a_proof_bound_to_another_funduq_is_refused(settings: CoreSettings):
    from funduq.errors import InvalidRegistration

    funduq = _funduq_with_identity(settings)
    try:
        identity = ProviderIdentity.generate()
        await _register(funduq, identity, "relayed")

        challenge = funduq.issue_connect_challenge()
        the_funduq_it_meant = ProviderIdentity.generate().public_key
        proof = identity.sign_connect(the_funduq_it_meant, challenge, "pn", ["relayed"])

        with pytest.raises(InvalidRegistration, match="invalid connect proof"):
            await funduq.attach_provider(
                _link(funduq, identity),
                ["relayed"],
                challenge=challenge,
                provider_nonce="pn",
                proof=proof,
            )
    finally:
        await funduq.aclose()
