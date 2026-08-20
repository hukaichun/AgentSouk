from __future__ import annotations

import pytest

from funduq_provider_sdk import InvalidChain, ProviderIdentity, verify_chain

USER = {"type": "user", "id": "employee_x"}


def test_a_chain_verifies_and_names_each_hop_in_order():
    a, b = ProviderIdentity.generate(), ProviderIdentity.generate()
    chain = b.extend_chain(a.new_chain(USER))

    result = verify_chain(chain)

    assert result.subject == USER
    assert result.actor_public_keys == [a.public_key, b.public_key]


def test_an_empty_chain_is_invalid():
    with pytest.raises(InvalidChain):
        verify_chain([])


def test_a_grafted_hop_from_another_chain_is_rejected():
    a, b = ProviderIdentity.generate(), ProviderIdentity.generate()
    real = a.new_chain(USER)
    foreign = b.new_chain(USER)
    grafted = b.sign_hop(USER, prev_token=foreign[0])

    with pytest.raises(InvalidChain, match="prevHash"):
        verify_chain([*real, grafted])


def test_a_swapped_subject_is_rejected():
    a, b = ProviderIdentity.generate(), ProviderIdentity.generate()
    chain = a.new_chain(USER)
    swapped = b.sign_hop({"type": "user", "id": "someone_else"}, prev_token=chain[0])

    with pytest.raises(InvalidChain, match="subject"):
        verify_chain([*chain, swapped])


def test_an_expired_last_hop_is_rejected_but_expired_middle_hops_are_fine():
    a, b = ProviderIdentity.generate(), ProviderIdentity.generate()
    stale = a.sign_hop(USER, ttl=-10)

    with pytest.raises(InvalidChain, match="expiry"):
        verify_chain([stale])

    fresh_on_top = [stale, b.sign_hop(USER, prev_token=stale)]
    assert verify_chain(fresh_on_top).actor_public_keys == [a.public_key, b.public_key]
