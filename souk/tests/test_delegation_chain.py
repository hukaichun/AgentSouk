"""A delegation chain must survive every hop, and resist being altered.

souk cannot know how the first agent came to trust a user — SSO, an internal
login, something else entirely — and does not try to. What it guarantees is
narrower and checkable: that a claim, once made, can be carried through any
number of hops, and that nobody can rewrite it on the way.

Both halves have to be in core for that to hold. Verifying was already there.
Extending was not: it lived only in souk-agent-sdk, so an agent running
inside souk could receive a chain and had no way to pass it on — provenance
died at the first in-process hop. These tests pin both.
"""

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
    """The point of the whole mechanism: agency -> A -> B -> C, and the last
    callee can still see it is ultimately about the same user, plus exactly
    who relayed it."""
    agency, a, b = _key(), _key(), _key()

    chain = new_actor_chain(agency, USER)
    chain = extend_actor_chain(a, chain)
    chain = extend_actor_chain(b, chain)

    result = verify_actor_chain(chain)
    assert result.subject == USER
    # Ordered oldest to newest: who started it, then each relay in turn.
    assert result.actor_public_keys == [_hex(agency), _hex(a), _hex(b)]


def test_souk_does_not_vouch_for_the_subject_itself():
    """souk verifies who signed, never that the claim is true. An agent can
    assert any subject it likes; what it cannot do is assert one nobody
    signed for, or alter one somebody else signed."""
    chain = new_actor_chain(_key(), {"type": "user", "id": "anyone-at-all"})
    assert verify_actor_chain(chain).subject == {"type": "user", "id": "anyone-at-all"}


def test_a_forged_hop_is_rejected():
    """Claiming someone else's public key without their private key."""
    victim, forger = _key(), _key()
    now = int(time.time())
    forged = jwt.encode(
        {
            "subject": USER,
            "actorPublicKey": _hex(victim),  # claims to be the victim
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
    """Each hop commits to the exact previous one, so hops cannot be
    reordered, truncated, or grafted in from a different chain."""
    agency, a = _key(), _key()
    real = new_actor_chain(agency, USER)
    elsewhere = new_actor_chain(_key(), USER)
    # A genuine hop by `a`, but chained onto a different origin.
    grafted = extend_actor_chain(a, elsewhere)[-1]

    with pytest.raises(InvalidActorChain):
        verify_actor_chain([*real, grafted])


def test_the_subject_cannot_be_swapped_partway():
    """Someone mid-chain cannot escalate what the call is on behalf of."""
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
    """Earlier hops are provenance, not standing authorization. A run paused
    on a human for longer than a hop's TTL must still be resumable, and a
    long delegation must not expire out from under normal latency — so an
    old inner hop is still fully signature-checked, just not expiry-checked.
    """
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
    """The converse: the hop representing "who is using this right now" does
    have to be current."""
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
    """Originating and relaying are different acts; conflating them would
    silently produce a chain with no origin."""
    with pytest.raises(ValueError):
        extend_actor_chain(_key(), [])


def test_core_and_sdk_produce_interoperable_chains(new_identity):
    """souk-agent-sdk deliberately doesn't depend on souk (a provider should
    not have to install the gateway), so each implements the hop format
    itself. tests/conftest.py's Identity mirrors the SDK exactly — a chain
    started there and extended by core must verify, or a remote provider and
    an in-process one could not appear in the same chain.
    """
    remote = new_identity()
    sdk_chain = [remote.sign_chain_hop(USER)]

    in_process = _key()
    chain = extend_actor_chain(in_process, sdk_chain)

    result = verify_actor_chain(chain)
    assert result.subject == USER
    assert result.actor_public_keys == [remote.public_key, _hex(in_process)]
