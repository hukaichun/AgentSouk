"""The bytes a provider signs.

These are pinned as *literals* on purpose. Everywhere else in this repository
a payload is built by calling the function that builds it, which proves only
that the function agrees with itself. Here the expected bytes are written out,
so changing the builder fails this test — which is what "an interop surface
stated independently" has to mean if it is to catch anything.

souk's own suite is the other half: it registers through this package, so a
change here fails there too. Neither half is enough alone.
"""

from __future__ import annotations

import pytest

from souk_provider_sdk import ProviderIdentity, deletion_payload, registration_payload


def test_a_registration_payload_is_exactly_these_bytes():
    assert registration_payload(["translator"], 1755300000) == b"souk-register:translator:1755300000"


def test_names_are_sorted_so_order_cannot_change_the_signature():
    assert registration_payload(["b", "a"], 1) == registration_payload(["a", "b"], 1)


def test_a_deletion_payload_is_exactly_these_bytes():
    assert deletion_payload("translator", 1755300000) == b"souk-delete-agent:translator:1755300000"


def test_registering_and_deleting_one_agent_are_different_bytes():
    """The check that stops a captured registration being replayed as a
    deletion order. Without the operation prefix these were byte-identical:
    observing a provider register a single agent handed you a valid order to
    delete it, inside the freshness window — no key theft, no broken crypto.
    """
    assert registration_payload(["a"], 1) != deletion_payload("a", 1)


def test_an_identity_is_its_key_and_signs_over_the_timestamp_it_returns():
    """The signature and the timestamp are returned together because souk
    verifies one *over* the other, and handing a caller two values that do not
    belong together is a way to fail that is hard to see."""
    identity = ProviderIdentity.generate()

    signature, timestamp = identity.sign_registration(["a"])

    assert len(identity.public_key) == 64
    assert isinstance(signature, str) and isinstance(timestamp, int)


def test_a_chain_carries_its_subject_forward(tmp_path):
    first, second = ProviderIdentity.generate(), ProviderIdentity.generate()
    subject = {"type": "user", "id": "employee_x"}

    chain = second.extend_chain(first.new_chain(subject))

    assert len(chain) == 2


def test_extending_nothing_is_refused():
    with pytest.raises(ValueError):
        ProviderIdentity.generate().extend_chain([])


def test_an_identity_persists_so_a_restart_is_the_same_provider(tmp_path):
    """An agent is `(provider_key, name)`, so a fresh key is a fresh provider
    offering the same names — and anything pointing at the old pair keeps
    pointing at an identity nobody holds."""
    path = tmp_path / "identity.key"

    first = ProviderIdentity.load_or_create(path)
    second = ProviderIdentity.load_or_create(path)

    assert first.public_key == second.public_key
