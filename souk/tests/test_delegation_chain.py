from __future__ import annotations

import hashlib
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk.identity import (
    InvalidActorChain,
    extend_actor_chain,
    new_actor_chain,
    verify_actor_chain,
)

USER = {"type": "user", "id": "employee_x"}


def _key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def _hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes_raw().hex()


def test_a_chain_survives_several_hops():
    agency, a, b = _key(), _key(), _key()

    chain = new_actor_chain(agency, USER)
    chain = extend_actor_chain(a, chain)
    chain = extend_actor_chain(b, chain)

    result = verify_actor_chain(chain)
    assert result.subject == USER
    assert result.actor_public_keys == [_hex(agency), _hex(a), _hex(b)]


def test_souk_does_not_vouch_for_the_subject_itself():
    chain = new_actor_chain(_key(), {"type": "user", "id": "anyone-at-all"})
    assert verify_actor_chain(chain).subject == {"type": "user", "id": "anyone-at-all"}


def test_a_forged_hop_is_rejected():
    victim, forger = _key(), _key()
    now = int(time.time())
    forged = jwt.encode(
        {
            "subject": USER,
            "actorPublicKey": _hex(victim),
            "prevHash": None,
            "iat": now,
            "exp": now + 300,
        },
        forger,
        algorithm="EdDSA",
    )
    with pytest.raises(InvalidActorChain):
        verify_actor_chain([forged])


def test_a_spliced_chain_is_rejected():
    agency, a = _key(), _key()
    real = new_actor_chain(agency, USER)
    elsewhere = new_actor_chain(_key(), USER)
    grafted = extend_actor_chain(a, elsewhere)[-1]

    with pytest.raises(InvalidActorChain):
        verify_actor_chain([*real, grafted])


def test_the_subject_cannot_be_swapped_partway():
    agency, a = _key(), _key()
    chain = new_actor_chain(agency, USER)
    now = int(time.time())
    swapped = jwt.encode(
        {
            "subject": {"type": "user", "id": "the_ceo"},
            "actorPublicKey": _hex(a),
            "prevHash": hashlib.sha256(chain[-1].encode()).hexdigest(),
            "iat": now,
            "exp": now + 300,
        },
        a,
        algorithm="EdDSA",
    )
    with pytest.raises(InvalidActorChain):
        verify_actor_chain([*chain, swapped])


def test_only_the_last_hop_has_to_be_fresh():
    agency, a = _key(), _key()
    now = int(time.time())
    stale_origin = jwt.encode(
        {"subject": USER, "actorPublicKey": _hex(agency), "prevHash": None,
         "iat": now - 7200, "exp": now - 3600},
        agency,
        algorithm="EdDSA",
    )
    chain = extend_actor_chain(a, [stale_origin])

    assert verify_actor_chain(chain).subject == USER


def test_an_expired_last_hop_is_rejected():
    agency = _key()
    now = int(time.time())
    expired = jwt.encode(
        {"subject": USER, "actorPublicKey": _hex(agency), "prevHash": None,
         "iat": now - 7200, "exp": now - 3600},
        agency,
        algorithm="EdDSA",
    )
    with pytest.raises(InvalidActorChain):
        verify_actor_chain([expired])


def test_extending_nothing_is_an_error():
    with pytest.raises(ValueError):
        extend_actor_chain(_key(), [])


def test_the_sdk_verifier_agrees_with_core_in_both_directions(new_identity):
    from souk_provider_sdk import InvalidChain, verify_chain

    core_key, sdk_identity = _key(), new_identity()

    core_chain = extend_actor_chain(core_key, [sdk_identity.sign_chain_hop(USER)])
    ours, theirs = verify_actor_chain(core_chain), verify_chain(core_chain)
    assert (ours.subject, ours.actor_public_keys) == (theirs.subject, theirs.actor_public_keys)

    foreign = new_actor_chain(_key(), USER)
    grafted = extend_actor_chain(core_key, foreign)[-1]
    tampered = [*core_chain, grafted]
    with pytest.raises(InvalidActorChain):
        verify_actor_chain(tampered)
    with pytest.raises(InvalidChain):
        verify_chain(tampered)


def test_core_and_sdk_produce_interoperable_chains(new_identity):
    remote = new_identity()
    sdk_chain = [remote.sign_chain_hop(USER)]

    in_process = _key()
    chain = extend_actor_chain(in_process, sdk_chain)

    result = verify_actor_chain(chain)
    assert result.subject == USER
    assert result.actor_public_keys == [remote.public_key, _hex(in_process)]
