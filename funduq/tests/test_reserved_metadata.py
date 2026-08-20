"""Caller metadata cannot wear funduq's handwriting.

funduq writes a small set of keys into a run's metadata record
(`verifiedActorChain`, `interrupts`, `failureReason`). A caller-supplied
value under any of them is stripped at the doors — otherwise a caller could
plant a forged verification summary, or a fake failure reason, that later
readers would take for funduq's own record. Keys funduq does not write are
plain caller data and pass through untouched.
"""

from __future__ import annotations

from funduq import repo
from funduq.protocols.a2a import A2AAdapter

from tests.conftest import EchoAgent


def _message(text: str) -> dict:
    return {"role": "user", "parts": [{"type": "text", "text": text}]}


async def test_a_forged_verified_actor_chain_is_stripped_at_the_door(funduq, serve):
    served = await serve(EchoAgent(), "audited")
    agent = served.agents["audited"]

    sent = await A2AAdapter(funduq).send_task(
        agent,
        _message("hi"),
        metadata={
            "verifiedActorChain": {"subject": "totally-legit", "actors": []},
            "keep": "this",
        },
    )

    async with funduq.session() as session:
        stored = await repo.get_run(session, sent["id"])
    assert "verifiedActorChain" not in stored.metadata, (
        "no chain was attached, so no verification summary may exist"
    )
    assert stored.metadata.get("keep") == "this", "only reserved keys are stripped"


async def test_a_real_actor_chain_still_earns_its_summary(funduq, serve, new_identity):
    served = await serve(EchoAgent(), "vouched")
    agent = served.agents["vouched"]
    chain = [new_identity().sign_chain_hop({"userId": "u-1"})]

    sent = await A2AAdapter(funduq).send_task(
        agent,
        _message("hi"),
        actor_chain=chain,
        metadata={"verifiedActorChain": "forged-and-overridden"},
    )

    async with funduq.session() as session:
        stored = await repo.get_run(session, sent["id"])
    assert stored.metadata["verifiedActorChain"]["subject"] == {"userId": "u-1"}


async def test_only_funduq_written_keys_are_stripped(funduq, serve):
    served = await serve(EchoAgent(), "unannotated")
    agent = served.agents["unannotated"]

    sent = await A2AAdapter(funduq).send_task(
        agent,
        _message("hi"),
        metadata={"addressedRunId": "run_i_made_up", "failureReason": "not yours to say"},
    )

    async with funduq.session() as session:
        stored = await repo.get_run(session, sent["id"])
    assert "failureReason" not in stored.metadata, "funduq writes this key; forgery is stripped"
    assert stored.metadata.get("addressedRunId") == "run_i_made_up", (
        "funduq does not write this key into run records, so it is plain caller data"
    )
