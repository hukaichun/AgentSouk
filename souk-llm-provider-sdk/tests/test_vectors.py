from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from souk_llm_provider_sdk import ProviderIdentity, llm_registration_payload

VECTORS = json.loads((Path(__file__).parent.parent.parent / "docs" / "contract-vectors.json").read_text())


def test_this_side_reproduces_the_llm_registration_vector():
    identity = ProviderIdentity(
        Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(VECTORS["test_key"]["private_key_hex"])
        )
    )
    (vector,) = [v for v in VECTORS["vectors"] if v["kind"] == "llm-registration"]

    payload = llm_registration_payload(vector["inputs"]["names"], vector["inputs"]["timestamp"])

    assert payload == vector["payload_utf8"].encode()
    assert identity.sign(payload) == vector["signature_hex"]
