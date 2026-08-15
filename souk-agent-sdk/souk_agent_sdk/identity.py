"""A provider's identity to any souk it connects to is its Ed25519
keypair — not an account souk issues, see souk/identity.py on the server
side. Generated once and persisted to disk so restarting this process
still owns the same agent_ids (a fresh key is a fresh, unrelated identity —
souk lets it register under the same `name`s without complaint, since names
aren't exclusive, but it gets *new* agent_ids with no connection to the old
ones; anything still pointed at the old agent_ids, e.g. another provider's
sub-agent delegation config, keeps talking to the orphaned identity, not
this one).

Losing this file means losing the ability to update those agent
registrations under their original agent_ids — there's no recovery flow in
this minimal version, so treat it like any other credential (back it up,
don't commit it).
"""

from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Deliberately more generous than SIGNATURE_FRESHNESS_WINDOW_SECONDS
# used elsewhere (60s) — an actor chain is typically built once at the
# start of a run and only actually used if/when the agent decides to
# call a tool that delegates further, which can be well after an LLM's
# own thinking time, not immediately.
ACTOR_CHAIN_TTL_SECONDS = 300


def load_or_create_identity(path: str | Path) -> Ed25519PrivateKey:
    path = Path(path)
    if path.exists():
        return Ed25519PrivateKey.from_private_bytes(path.read_bytes())
    private_key = Ed25519PrivateKey.generate()
    raw = private_key.private_bytes_raw()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    return private_key


def public_key_hex(private_key: Ed25519PrivateKey) -> str:
    return private_key.public_key().public_bytes_raw().hex()


def sign(private_key: Ed25519PrivateKey, payload: bytes) -> str:
    return private_key.sign(payload).hex()


def registration_signing_payload(agent_names: list[str], timestamp: int) -> bytes:
    # Must match souk.identity.registration_signing_payload exactly —
    # souk verifies this signature against the same canonical string.
    # `timestamp` (unix seconds) must be current — souk rejects anything
    # outside its freshness window, so a captured signed request can't be
    # replayed indefinitely (see souk.identity.SIGNATURE_FRESHNESS_WINDOW_SECONDS).
    # The identity is not in here: the signature is verified against the
    # public key sent alongside, which is what ties this request to it.
    return f"{','.join(sorted(agent_names))}:{timestamp}".encode()


def _sign_hop(private_key: Ed25519PrivateKey, subject: dict, prev_token: str | None) -> str:
    now = int(time.time())
    payload = {
        "subject": subject,
        "actorPublicKey": public_key_hex(private_key),
        "prevHash": hashlib.sha256(prev_token.encode()).hexdigest() if prev_token is not None else None,
        "iat": now,
        "exp": now + ACTOR_CHAIN_TTL_SECONDS,
    }
    return jwt.encode(payload, private_key, algorithm="EdDSA")


def new_actor_chain(private_key: Ed25519PrivateKey, subject: dict) -> list[str]:
    """Starts a fresh identity chain — see souk.identity.verify_actor_chain
    for how souk verifies it and why it's a chain, not a single signature.

    `subject` is who this chain is fundamentally about. For a provider
    calling another agent on its own behalf (e.g.
    pydantic_ai_agent.sub_agent_tool), that's just itself:
    `{"type": "agent", "publicKey": public_key_hex(private_key)}`. An
    "agency" agent that authenticated a human user by its own means (SSO,
    an internal login, ...) and now wants to make calls on that user's
    behalf would instead pass e.g.
    `{"type": "user", "id": "employee_x"}` — souk doesn't verify that
    claim itself (it has no way to), it only verifies that whoever signs
    each hop of the chain really holds the key it claims to.
    """
    return [_sign_hop(private_key, subject, None)]


def extend_actor_chain(private_key: Ed25519PrivateKey, prev_chain: list[str]) -> list[str]:
    """Adds one more hop to a chain this provider *received* as a caller
    (e.g. forwarded to it via A2A/AG-UI metadata) and is now relaying
    onward — the chain's `subject` (see souk.identity.verify_actor_chain)
    is read from the last hop as-is, unverified locally; souk is the one
    that actually checks the whole chain's integrity when this is used.
    """
    if not prev_chain:
        raise ValueError("extend_actor_chain requires a non-empty prev_chain — use new_actor_chain to originate one")
    subject = jwt.decode(prev_chain[-1], options={"verify_signature": False})["subject"]
    return [*prev_chain, _sign_hop(private_key, subject, prev_chain[-1])]
