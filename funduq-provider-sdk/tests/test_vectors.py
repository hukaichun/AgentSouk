from __future__ import annotations

import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from funduq_provider_sdk import (
    ProviderIdentity,
    provider_connect_payload,
    funduq_connect_payload,
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
    "provider-connect": lambda i: provider_connect_payload(
        i["funduq_public_key"], i["funduq_nonce"], i["provider_nonce"], i["names"]
    ),
    "funduq-connect": lambda i: funduq_connect_payload(i["funduq_nonce"], i["provider_nonce"]),
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


def test_the_delivered_run_frame_round_trips_through_the_declared_model():
    from funduq_provider_sdk import DeliveredRun

    (frame,) = [w["frame"] for w in VECTORS["wire"] if w["kind"] == "delivered-run"]

    model = DeliveredRun.model_validate(frame)

    assert model.agent_name == frame["agentName"]
    assert model.run_input.thread_id == frame["runInput"]["threadId"]
    assert model.model_dump(mode="json", by_alias=True) == frame


def test_the_published_chain_verifies_here_too_and_can_be_reproduced():
    import jwt as _jwt
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as _Key

    from funduq_provider_sdk import verify_chain

    (vector,) = [c for c in VECTORS["chains"] if c["kind"] == "actor-chain"]

    result = verify_chain(vector["chain"])
    assert result.subject == vector["subject"]
    assert result.actor_public_keys == vector["actor_public_keys"]

    first_key = _Key.from_private_bytes(bytes.fromhex(vector["inputs"]["hop_private_keys_hex"][0]))
    reproduced = _jwt.encode(
        {
            "subject": vector["inputs"]["subject"],
            "actorPublicKey": vector["actor_public_keys"][0],
            "prevHash": None,
            "iat": vector["inputs"]["iat"],
            "exp": vector["inputs"]["exp"],
        },
        first_key,
        algorithm="EdDSA",
    )
    assert reproduced == vector["chain"][0]


def test_every_funduq_invented_wire_structure_validates_with_this_packages_models():
    """The SDK-side dual of the tag-scan guard: funduq puts nothing on the wire this package cannot validate.

    A hand-restated copy of these shapes drifted once on one field's
    nullability and silently dropped verified caller identities; the twins
    plus this frame are what make restating unnecessary.
    """
    from funduq_provider_sdk import CallerProps, KyokForwardedProps

    (frame,) = [w["frame"] for w in VECTORS["wire"] if w["kind"] == "delivered-run"]
    props = frame["runInput"]["forwardedProps"]
    funduq_keys = {"caller": CallerProps, "kyok": KyokForwardedProps}

    assert funduq_keys.keys() <= props.keys(), "the published frame must carry every declared key"
    for key, model in funduq_keys.items():
        parsed = model.model_validate(props[key])
        assert parsed.model_dump(mode="json", by_alias=True) == props[key], key
    assert CallerProps.model_validate(props["caller"]).chain is None
