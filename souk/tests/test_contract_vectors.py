from __future__ import annotations

import json
from pathlib import Path

from souk.identity import (
    agent_deletion_signing_payload,
    provider_connect_signing_payload,
    souk_connect_signing_payload,
    llm_deletion_signing_payload,
    kyok_call_signing_payload,
    llm_registration_signing_payload,
    registration_signing_payload,
    verify_signature,
)

VECTORS = json.loads((Path(__file__).parent.parent.parent / "docs" / "contract-vectors.json").read_text())

BUILDERS = {
    "agent-registration": lambda i: registration_signing_payload(i["names"], i["timestamp"]),
    "llm-registration": lambda i: llm_registration_signing_payload(i["names"], i["timestamp"]),
    "agent-deletion": lambda i: agent_deletion_signing_payload(i["agent_name"], i["timestamp"]),
    "llm-deletion": lambda i: llm_deletion_signing_payload(i["name"], i["timestamp"]),
    "provider-connect": lambda i: provider_connect_signing_payload(
        i["souk_nonce"], i["provider_nonce"], i["names"]
    ),
    "souk-connect": lambda i: souk_connect_signing_payload(i["souk_nonce"], i["provider_nonce"]),
    "kyok-call": lambda i: kyok_call_signing_payload(
        i["bearer"], i["timestamp"], i["body_sha256_hex"]
    ),
}


def test_every_published_vector_is_what_this_implementation_computes():
    assert {v["kind"] for v in VECTORS["vectors"]} == set(BUILDERS)
    for vector in VECTORS["vectors"]:
        payload = BUILDERS[vector["kind"]](vector["inputs"])
        assert payload == vector["payload_utf8"].encode(), vector["kind"]
        assert verify_signature(
            VECTORS["test_key"]["public_key_hex"], vector["signature_hex"], payload
        ), vector["kind"]


def test_registration_vectors_do_not_depend_on_name_order():
    for vector in VECTORS["vectors"]:
        if "names" in vector["inputs"]:
            shuffled = list(reversed(vector["inputs"]["names"]))
            assert BUILDERS[vector["kind"]](
                {**vector["inputs"], "names": shuffled}
            ) == vector["payload_utf8"].encode()


def test_every_domain_tag_has_a_published_vector_family():
    """The completeness guard: an unpublished payload family fails here, not in an integrator's transport."""
    import re

    from souk import identity as identity_module

    source = Path(identity_module.__file__).read_text()
    tags = set(re.findall(r'= "(souk-[a-z-]+)"', source))
    assert tags, "no domain tags found — the scan is broken, not the contract"
    covered = {v["payload_utf8"].split(":", 1)[0] for v in VECTORS["vectors"]}
    assert tags <= covered, (
        f"domain tags without a vector family in docs/contract-vectors.json: {sorted(tags - covered)}. "
        "Whoever states the bytes publishes the vectors for them."
    )
