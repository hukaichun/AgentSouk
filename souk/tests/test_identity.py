from __future__ import annotations

import pytest

from souk.identity import InvalidActorChain, verify_actor_chain


def test_empty_chain_is_rejected(new_identity):
    with pytest.raises(InvalidActorChain, match="empty"):
        verify_actor_chain([])


def test_single_hop_chain_verifies(new_identity):
    identity = new_identity()
    subject = {"type": "agent", "id": "a1"}
    chain = [identity.sign_chain_hop(subject)]

    result = verify_actor_chain(chain)

    assert result.subject == subject
    assert result.actor_public_keys == [identity.public_key]


def test_multi_hop_chain_verifies_in_order(new_identity):
    a, b, c = new_identity(), new_identity(), new_identity()
    subject = {"type": "user", "id": "employee_x"}
    hop0 = a.sign_chain_hop(subject)
    hop1 = b.sign_chain_hop(subject, prev_token=hop0)
    hop2 = c.sign_chain_hop(subject, prev_token=hop1)

    result = verify_actor_chain([hop0, hop1, hop2])

    assert result.subject == subject
    assert result.actor_public_keys == [a.public_key, b.public_key, c.public_key]


def test_reordered_hops_rejected(new_identity):
    a, b = new_identity(), new_identity()
    subject = {"type": "agent", "id": "a1"}
    hop0 = a.sign_chain_hop(subject)
    hop1 = b.sign_chain_hop(subject, prev_token=hop0)

    with pytest.raises(InvalidActorChain, match="prevHash"):
        verify_actor_chain([hop1, hop0])


def test_spliced_hop_from_different_chain_rejected(new_identity):
    a, b, foreign = new_identity(), new_identity(), new_identity()
    subject = {"type": "agent", "id": "a1"}
    hop0 = a.sign_chain_hop(subject)
    hop1 = b.sign_chain_hop(subject, prev_token=hop0)
    foreign_hop0 = foreign.sign_chain_hop(subject)
    spliced_hop1 = b.sign_chain_hop(subject, prev_token=foreign_hop0)

    with pytest.raises(InvalidActorChain, match="prevHash"):
        verify_actor_chain([hop0, spliced_hop1])


def test_subject_change_partway_through_rejected(new_identity):
    a, b = new_identity(), new_identity()
    hop0 = a.sign_chain_hop({"type": "agent", "id": "a1"})
    hop1 = b.sign_chain_hop({"type": "agent", "id": "a2"}, prev_token=hop0)

    with pytest.raises(InvalidActorChain, match="subject changed"):
        verify_actor_chain([hop0, hop1])


def test_historical_hop_expiry_is_ignored(new_identity):
    a, b = new_identity(), new_identity()
    subject = {"type": "agent", "id": "a1"}
    expired_hop0 = a.sign_chain_hop(subject, exp_offset=-3600)
    fresh_hop1 = b.sign_chain_hop(subject, prev_token=expired_hop0, exp_offset=300)

    result = verify_actor_chain([expired_hop0, fresh_hop1])

    assert result.subject == subject


def test_last_hop_expiry_is_enforced(new_identity):
    a, b = new_identity(), new_identity()
    subject = {"type": "agent", "id": "a1"}
    hop0 = a.sign_chain_hop(subject, exp_offset=300)
    expired_hop1 = b.sign_chain_hop(subject, prev_token=hop0, exp_offset=-3600)

    with pytest.raises(InvalidActorChain, match="expiry"):
        verify_actor_chain([hop0, expired_hop1])


def test_single_expired_hop_chain_rejected(new_identity):
    identity = new_identity()
    chain = [identity.sign_chain_hop({"type": "agent", "id": "a1"}, exp_offset=-60)]

    with pytest.raises(InvalidActorChain, match="expiry"):
        verify_actor_chain(chain)
