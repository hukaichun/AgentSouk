from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk_provider_sdk import (
    ProviderIdentity,
    deletion_payload,
    kyok_call_payload,
    registration_payload,
    verify_signature,
)

VECTORS = json.loads((Path(__file__).parent.parent.parent / "docs" / "contract-vectors.json").read_text())

BUILDERS = {
    "agent-registration": lambda i: registration_payload(i["names"], i["timestamp"]),
    "agent-deletion": lambda i: deletion_payload(i["agent_name"], i["timestamp"]),
    "kyok-call": lambda i: kyok_call_payload(i["bearer"], i["timestamp"], i["body_sha256_hex"]),
}


def _identity() -> ProviderIdentity:
    return ProviderIdentity(
        Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(VECTORS["test_key"]["private_key_hex"])
        )
    )


def test_this_side_reproduces_every_vector_it_has_a_builder_for():
    identity = _identity()
    assert identity.public_key == VECTORS["test_key"]["public_key_hex"]
    covered = 0
    for vector in VECTORS["vectors"]:
        builder = BUILDERS.get(vector["kind"])
        if builder is None:
            continue
        covered += 1
        payload = builder(vector["inputs"])
        assert payload == vector["payload_utf8"].encode(), vector["kind"]
        assert identity.sign(payload) == vector["signature_hex"], vector["kind"]
        assert verify_signature(identity.public_key, vector["signature_hex"], payload)
    assert covered == len(BUILDERS)
