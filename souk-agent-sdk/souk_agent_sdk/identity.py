"""A provider's identity to any souk it connects to is its Ed25519
keypair — not an account souk issues, see souk/identity.py on the server
side. Generated once and persisted to disk so restarting this process
still owns the same agent names (a fresh key would be a fresh, unrelated
identity — souk would refuse to let it re-claim names the old key owned).

Losing this file means losing the ability to update those agent
registrations — there's no recovery flow in this minimal version, so
treat it like any other credential (back it up, don't commit it).
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


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


def registration_signing_payload(sdk_client_id: str, agent_names: list[str]) -> bytes:
    # Must match souk.identity.registration_signing_payload exactly —
    # souk verifies this signature against the same canonical string.
    return f"{sdk_client_id}:{','.join(sorted(agent_names))}".encode()
