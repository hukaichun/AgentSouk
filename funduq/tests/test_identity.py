from __future__ import annotations

import pytest

from funduq.identity import InvalidActorChain, verify_actor_chain


def test_empty_chain_is_rejected(new_identity):
    with pytest.raises(InvalidActorChain, match="empty"):
        verify_actor_chain([])


def test_single_hop_chain_verifies(new_identity):
    identity = new_identity()
    chain = [identity.sign_chain_hop()]

    result = verify_actor_chain(chain)

    assert result.actor_public_keys == [identity.public_key]
    assert result.head == identity.public_key


def test_multi_hop_chain_verifies_in_order(new_identity):
    a, b, c = new_identity(), new_identity(), new_identity()
    hop0 = a.sign_chain_hop()
    hop1 = b.sign_chain_hop(prev_token=hop0)
    hop2 = c.sign_chain_hop(prev_token=hop1)

    result = verify_actor_chain([hop0, hop1, hop2])

    assert result.actor_public_keys == [a.public_key, b.public_key, c.public_key]
    assert result.head == a.public_key, "the first signer is the segment's authority"


def test_reordered_hops_rejected(new_identity):
    a, b = new_identity(), new_identity()
    hop0 = a.sign_chain_hop()
    hop1 = b.sign_chain_hop(prev_token=hop0)

    with pytest.raises(InvalidActorChain, match="prevHash"):
        verify_actor_chain([hop1, hop0])


def test_spliced_hop_from_different_chain_rejected(new_identity):
    a, b, foreign = new_identity(), new_identity(), new_identity()
    hop0 = a.sign_chain_hop()
    hop1 = b.sign_chain_hop(prev_token=hop0)
    foreign_hop0 = foreign.sign_chain_hop()
    spliced_hop1 = b.sign_chain_hop(prev_token=foreign_hop0)

    with pytest.raises(InvalidActorChain, match="prevHash"):
        verify_actor_chain([hop0, spliced_hop1])


def test_a_legacy_hop_still_stamping_subject_verifies(new_identity):
    """`subject` was retired from the hop format: it was the signer's own
    unverifiable claim, and a chain carries keys and nothing else. A signer
    still stamping it produces an unknown claim, which is ignored."""
    import time

    import jwt

    identity = new_identity()
    now = int(time.time())
    legacy = jwt.encode(
        {
            "subject": {"type": "user", "id": "employee_x"},
            "actorPublicKey": identity.public_key,
            "prevHash": None,
            "iat": now,
            "exp": now + 300,
        },
        identity._private_key,
        algorithm="EdDSA",
    )
    assert verify_actor_chain([legacy]).head == identity.public_key


def test_historical_hop_expiry_is_ignored(new_identity):
    a, b = new_identity(), new_identity()
    expired_hop0 = a.sign_chain_hop(exp_offset=-3600)
    fresh_hop1 = b.sign_chain_hop(prev_token=expired_hop0, exp_offset=300)

    result = verify_actor_chain([expired_hop0, fresh_hop1])

    assert result.head == a.public_key


def test_last_hop_expiry_is_enforced(new_identity):
    a, b = new_identity(), new_identity()
    hop0 = a.sign_chain_hop(exp_offset=300)
    expired_hop1 = b.sign_chain_hop(prev_token=hop0, exp_offset=-3600)

    with pytest.raises(InvalidActorChain, match="expiry"):
        verify_actor_chain([hop0, expired_hop1])


def test_single_expired_hop_chain_rejected(new_identity):
    identity = new_identity()
    chain = [identity.sign_chain_hop(exp_offset=-60)]

    with pytest.raises(InvalidActorChain, match="expiry"):
        verify_actor_chain(chain)
