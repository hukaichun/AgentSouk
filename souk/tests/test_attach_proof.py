from __future__ import annotations

import pytest
from souk_provider_sdk import ProviderIdentity

from souk.core import Souk
from souk.errors import InvalidRegistration

from tests.conftest import DATABASE_URL, TEST_SIGNING_SECRET
from souk.config import CoreSettings


class _Stub:

    max_concurrent_runs = None

    def __init__(self, public_key: str) -> None:
        self.public_key = public_key

    async def deliver(self, run):
        return False

    def cancel(self, run_id: str) -> None:
        pass


class _Forged(_Stub):
    """Claims one identity's public key while holding a different private key."""

    def __init__(self, claimed_key: str, actual: ProviderIdentity) -> None:
        super().__init__(claimed_key)
        self._actual = actual

    def sign_connect(
        self, souk_public_key: str, souk_nonce: str, provider_nonce: str, names: list[str]
    ) -> str:
        return self._actual.sign_connect(souk_public_key, souk_nonce, provider_nonce, names)


async def _registered(souk, name: str) -> ProviderIdentity:
    identity = ProviderIdentity.generate()
    signature, timestamp = identity.sign_registration([name])
    await souk.register_agents(identity.public_key, signature, timestamp, [{"name": name}])
    return identity


async def test_a_connection_that_cannot_sign_for_its_claimed_key_is_rejected(souk):
    identity = await _registered(souk, "forged")
    imposter = _Forged(identity.public_key, ProviderIdentity.generate())

    with pytest.raises(InvalidRegistration, match="invalid connect proof"):
        await souk.attach_provider(imposter, ["forged"])


async def test_an_explicit_proof_must_answer_a_challenge_souk_issued(souk):
    identity = await _registered(souk, "replayer")
    stub = _Stub(identity.public_key)
    proof = identity.sign_connect("", "not-a-souk-challenge", "pn", ["replayer"])

    with pytest.raises(InvalidRegistration, match="live challenge"):
        await souk.attach_provider(
            stub, ["replayer"], challenge="not-a-souk-challenge", provider_nonce="pn", proof=proof
        )


async def test_a_challenge_is_single_use(souk):
    identity = await _registered(souk, "once")
    stub = _Stub(identity.public_key)
    challenge = souk.issue_connect_challenge()
    proof = identity.sign_connect("", challenge, "pn", ["once"])

    await souk.attach_provider(stub, ["once"], challenge=challenge, provider_nonce="pn", proof=proof)
    souk.detach_all_for(identity.public_key)
    with pytest.raises(InvalidRegistration, match="live challenge"):
        await souk.attach_provider(
            stub, ["once"], challenge=challenge, provider_nonce="pn", proof=proof
        )


async def test_a_silent_connection_is_rejected_and_the_signer_admitted():
    souk = Souk(
        CoreSettings(
            database_url=DATABASE_URL,
            token_signing_secret=TEST_SIGNING_SECRET,
        )
    )
    try:
        identity = await _registered(souk, "strict")

        with pytest.raises(InvalidRegistration, match="without a connect proof"):
            await souk.attach_provider(_Stub(identity.public_key), ["strict"])

        signer = _Forged(identity.public_key, identity)
        await souk.attach_provider(signer, ["strict"])
        from souk.models import AgentRef

        assert souk.is_serving(AgentRef(provider_key=identity.public_key, name="strict"))
    finally:
        await souk.aclose()
