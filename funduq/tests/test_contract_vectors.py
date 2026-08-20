from __future__ import annotations

import json
from pathlib import Path

from funduq.identity import (
    agent_deletion_signing_payload,
    provider_connect_signing_payload,
    funduq_connect_signing_payload,
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
        i["funduq_public_key"], i["funduq_nonce"], i["provider_nonce"], i["names"]
    ),
    "funduq-connect": lambda i: funduq_connect_signing_payload(i["funduq_nonce"], i["provider_nonce"]),
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

    from funduq import identity as identity_module

    source = Path(identity_module.__file__).read_text()
    tags = set(re.findall(r'= "(funduq-[a-z-]+)"', source))
    assert tags, "no domain tags found — the scan is broken, not the contract"
    covered = {v["payload_utf8"].split(":", 1)[0] for v in VECTORS["vectors"]}
    assert tags <= covered, (
        f"domain tags without a vector family in docs/contract-vectors.json: {sorted(tags - covered)}. "
        "Whoever states the bytes publishes the vectors for them."
    )


def test_the_published_wire_frames_are_what_the_ports_translate_to():
    from funduq.kyok import CompletionRequest
    from funduq.models import AgentRef, ClaimedRun
    from funduq_llm_provider_sdk import DeliveredCompletion
    from funduq_provider_sdk import DeliveredRun

    (run_wire,) = [w["frame"] for w in VECTORS["wire"] if w["kind"] == "delivered-run"]
    claimed = ClaimedRun(
        run_id=run_wire["runId"],
        agent=AgentRef(provider_key="ab" * 32, name=run_wire["agentName"]),
        thread_id=run_wire["threadId"],
        run_input=run_wire["runInput"],
    )
    assert DeliveredRun.from_claimed(claimed).model_dump(mode="json", by_alias=True) == run_wire

    (completion_wire,) = [
        w["frame"] for w in VECTORS["wire"] if w["kind"] == "delivered-completion"
    ]
    request = CompletionRequest(
        run_id=completion_wire["runId"],
        agent=AgentRef(
            provider_key=completion_wire["providerKey"], name=completion_wire["agentName"]
        ),
        body=completion_wire["body"],
        llm_name=completion_wire["llmName"],
        context=completion_wire["context"],
        actor_chain=completion_wire["actorChain"],
    )
    assert (
        DeliveredCompletion.from_request(request).model_dump(mode="json", by_alias=True)
        == completion_wire
    )


def test_the_published_chain_verifies_and_names_exactly_the_published_actors():
    from funduq.identity import verify_actor_chain

    (vector,) = [c for c in VECTORS["chains"] if c["kind"] == "actor-chain"]

    result = verify_actor_chain(vector["chain"])

    assert result.subject == vector["subject"]
    assert result.actor_public_keys == vector["actor_public_keys"]


def test_cross_party_formats_without_a_domain_tag_are_vectored_too():
    """The tag scan can't see formats that aren't tagged strings — this pins the rest by name."""
    assert {c["kind"] for c in VECTORS.get("chains", [])} == {"actor-chain"}
    assert {w["kind"] for w in VECTORS.get("wire", [])} == {"delivered-run", "delivered-completion"}


def test_both_sides_props_twins_validate_the_same_frame():
    from funduq_provider_sdk import CallerProps as SdkCaller
    from funduq_provider_sdk import KyokForwardedProps as SdkKyok

    from funduq.kyok import KyokForwardedProps
    from funduq.props import CallerProps

    (frame,) = [w["frame"] for w in VECTORS["wire"] if w["kind"] == "delivered-run"]
    props = frame["runInput"]["forwardedProps"]

    for model, twin, key in (
        (CallerProps, SdkCaller, "caller"),
        (KyokForwardedProps, SdkKyok, "kyok"),
    ):
        ours = model.model_validate(props[key]).model_dump(mode="json", by_alias=True)
        theirs = twin.model_validate(props[key]).model_dump(mode="json", by_alias=True)
        assert ours == theirs == props[key], key
