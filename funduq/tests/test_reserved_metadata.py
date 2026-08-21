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


async def test_funduq_authors_no_verification_summary(funduq, serve, new_identity):
    """funduq stopped summarizing chains: no `verifiedActorChain` digest is
    ever written, so there is no digest to forge either — a caller-supplied
    value under that name is plain caller data, and the chain itself reaches
    the record verbatim."""
    served = await serve(EchoAgent(), "audited")
    agent = served.agents["audited"]
    chain = [new_identity().sign_chain_hop()]

    sent = await A2AAdapter(funduq).send_task(
        agent,
        _message("hi"),
        actor_chain=chain,
        metadata={"verifiedActorChain": "not-funduqs-word", "keep": "this"},
    )

    async with funduq.session() as session:
        stored = await repo.get_run(session, sent["id"])
    assert stored.metadata.get("verifiedActorChain") == "not-funduqs-word", (
        "funduq writes no such key, so the caller's value passes through as caller data"
    )
    assert stored.metadata.get("keep") == "this"
    assert stored.metadata.get("actorChain") == chain, "the chain itself is the record"


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
