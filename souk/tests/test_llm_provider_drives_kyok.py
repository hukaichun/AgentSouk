from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import AsyncIterator

import pytest
from ag_ui.core import RunAgentInput, UserMessage
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openai.types.chat import ChatCompletionChunk

from souk import repo
from souk.errors import KyokRejected, LlmProviderNotFound
from souk.identity import new_actor_chain
from souk.kyok import read_kyok_forwarded_props
from souk.models import LlmRef
from souk.protocols.a2a import A2AAdapter
from souk.protocols.agui import AGUIAdapter
from souk.protocols.kyok import KyokAdapter
from souk_provider_sdk.identity import kyok_call_payload
from souk_llm_provider_sdk import (
    DeliveredCompletion,
    InProcessLLMProvider,
    ProviderIdentity,
    sign_llm_registration,
)

from tests.conftest import Identity


def _chunk(text: str, *, role: bool = False, finish: str | None = None) -> ChatCompletionChunk:
    delta: dict = {} if finish else {"content": text}
    if role:
        delta["role"] = "assistant"
    return ChatCompletionChunk.model_validate(
        {
            "id": "chatcmpl-stub",
            "object": "chat.completion.chunk",
            "created": 1755300000,
            "model": "stub-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
    )


class StubLLM:

    def __init__(self, answer: str = "hello world") -> None:
        self.answer = answer
        self.seen: list[DeliveredCompletion] = []
        self.refuse: Exception | None = None

    async def __call__(self, delivered: DeliveredCompletion) -> AsyncIterator[ChatCompletionChunk]:
        self.seen.append(delivered)
        if self.refuse is not None:
            raise self.refuse
        head, tail = self.answer[: len(self.answer) // 2], self.answer[len(self.answer) // 2:]
        yield _chunk(head, role=True)
        yield _chunk(tail)
        yield _chunk("", finish="stop")


@pytest.fixture
async def llm(souk):
    identity = ProviderIdentity.generate()
    signature, timestamp = sign_llm_registration(identity, ["gpt4"])
    await souk.register_llm_providers(identity.public_key, signature, timestamp, ["gpt4"])
    stub = StubLLM()
    await souk.attach_llm_provider(InProcessLLMProvider(identity, stub), ["gpt4"])
    ref = LlmRef(provider_key=identity.public_key, name="gpt4")
    yield stub, identity, ref
    souk.detach_llm_provider(identity.public_key)


class KyokTokenAgent:

    def __init__(self) -> None:
        self.token: str | None = None
        self.run_id: str | None = None
        self.got_token = asyncio.Event()
        self.release = asyncio.Event()

    async def run_stream(self, agent_name: str, run_input):
        ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_STARTED", **ids}
        grant = read_kyok_forwarded_props(run_input.forwarded_props)
        self.token = grant.token if grant else None
        self.run_id = run_input.run_id
        self.got_token.set()
        await self.release.wait()
        yield {"type": "RUN_FINISHED", **ids}


def _body(
    ref: LlmRef | None,
    thread_id: str = "t-kyok",
    *,
    context: dict | None = None,
) -> RunAgentInput:
    kwargs = {}
    if ref is not None:
        kyok: dict = {"llmProvider": {"providerKey": ref.provider_key, "name": ref.name}}
        if context is not None:
            kyok["context"] = context
        kwargs["metadata"] = {"kyok": kyok}
    return RunAgentInput(
        thread_id=thread_id,
        run_id="ignored",
        state={},
        messages=[UserMessage(id="m1", role="user", content="hi")],
        tools=[],
        context=[],
        forwarded_props={},
        **kwargs,
    )


def _signed_call(identity: Identity, token: str, body: bytes) -> dict:
    timestamp = int(time.time())
    signature = identity.sign(
        kyok_call_payload(token, timestamp, hashlib.sha256(body).hexdigest())
    )
    return {"timestamp": str(timestamp), "signature": signature}


def _completion_body(*, stream: bool = False) -> bytes:
    return json.dumps(
        {"model": "whatever", "messages": [{"role": "user", "content": "hi"}], "stream": stream}
    ).encode()


async def _run_with_token(souk, serve, agent: KyokTokenAgent, ref: LlmRef, **body_kwargs):
    served = await serve(agent, "kyok-agent")
    stream = await AGUIAdapter(souk).run(
        served.agents["kyok-agent"], _body(ref, **body_kwargs)
    )
    await asyncio.wait_for(agent.got_token.wait(), timeout=5)
    return served, stream


async def _finish(agent: KyokTokenAgent, stream) -> None:
    agent.release.set()
    async for _ in stream.events:
        pass


async def test_a_non_streaming_call_gets_the_collapsed_answer(souk, serve, llm):
    stub, _, ref = llm
    agent = KyokTokenAgent()
    served, stream = await _run_with_token(souk, serve, agent, ref)

    body = _completion_body(stream=False)
    relay = await KyokAdapter(souk).complete(
        agent.token, body, **_signed_call(served.identity, agent.token, body)
    )
    assert relay.stream_requested is False
    completion = await relay.collapsed()

    assert completion.choices[0].message.content == "hello world"
    assert completion.choices[0].finish_reason == "stop"
    await _finish(agent, stream)


async def test_the_policy_seam_is_shown_who_is_asking(souk, serve, llm):
    stub, _, ref = llm
    agent = KyokTokenAgent()
    served, stream = await _run_with_token(
        souk, serve, agent, ref, context={"voucher": "user-42"}
    )

    body = _completion_body()
    await (
        await KyokAdapter(souk).complete(
            agent.token, body, **_signed_call(served.identity, agent.token, body)
        )
    ).collapsed()

    [delivered] = stub.seen
    assert delivered.run_id == agent.run_id
    assert delivered.provider_key == served.identity.public_key
    assert delivered.agent_name == "kyok-agent"
    assert delivered.llm_name == "gpt4"
    assert delivered.context == {"voucher": "user-42"}
    assert delivered.body["messages"] == [{"role": "user", "content": "hi"}]
    await _finish(agent, stream)


async def test_the_callers_context_is_never_persisted(souk, serve, llm):
    _, _, ref = llm
    agent = KyokTokenAgent()
    _, stream = await _run_with_token(
        souk, serve, agent, ref, context={"secret": "kyok-ctx-secret"}
    )

    async with souk.session() as session:
        snapshot = await repo.get_thread_snapshot(session, stream.thread_id)
        run = await repo.get_run(session, agent.run_id)
    persisted = json.dumps(snapshot, default=str) + json.dumps(
        dict(run._asdict()) if hasattr(run, "_asdict") else run.__dict__, default=str
    )
    assert "kyok-ctx-secret" not in persisted
    await _finish(agent, stream)


async def test_a_delegated_run_inherits_binding_and_shows_its_chain(souk, serve, llm):
    stub, _, ref = llm
    parent = KyokTokenAgent()
    served, stream = await _run_with_token(
        souk, serve, parent, ref, context={"voucher": "user-42"}
    )

    class CalleeLLMCaller:

        def __init__(self) -> None:
            self.identity: Identity | None = None
            self.answer: str | None = None

        async def run_stream(self, agent_name: str, run_input):
            ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
            yield {"type": "RUN_STARTED", **ids}
            grant = read_kyok_forwarded_props(run_input.forwarded_props)
            assert grant, "delegated run should inherit a KYOK token"
            token = grant.token
            body = _completion_body()
            relay = await KyokAdapter(souk).complete(
                token, body, **_signed_call(self.identity, token, body)
            )
            self.answer = (await relay.collapsed()).choices[0].message.content
            yield {"type": "RUN_FINISHED", **ids}

    callee = CalleeLLMCaller()
    callee_served = await serve(callee, "sub-agent")
    callee.identity = callee_served.identity

    chain = new_actor_chain(Ed25519PrivateKey.generate(), {"type": "user", "id": "user-42"})
    await A2AAdapter(souk).send_task(
        callee_served.agents["sub-agent"],
        {"role": "user", "parts": [{"type": "text", "text": "delegated"}]},
        reference_task_ids=[parent.run_id],
        actor_chain=chain,
    )

    assert callee.answer == "hello world"
    delivered = stub.seen[-1]
    assert delivered.agent_name == "sub-agent"
    assert delivered.context == {"voucher": "user-42"}
    assert delivered.actor_chain == chain
    await _finish(parent, stream)


async def test_a_streaming_call_streams(souk, serve, llm):
    _, _, ref = llm
    agent = KyokTokenAgent()
    served, stream = await _run_with_token(souk, serve, agent, ref)

    body = _completion_body(stream=True)
    relay = await KyokAdapter(souk).complete(
        agent.token, body, **_signed_call(served.identity, agent.token, body)
    )
    assert relay.stream_requested is True
    payloads = [p async for p in relay.encode()]

    assert payloads[-1] == "[DONE]"
    deltas = [
        json.loads(p)["choices"][0]["delta"].get("content") for p in payloads[:-1]
    ]
    assert "".join(d for d in deltas if d) == "hello world"
    await _finish(agent, stream)


async def test_a_policy_refusal_reaches_the_agent_as_a_502(souk, serve, llm):
    stub, _, ref = llm
    stub.refuse = PermissionError("quota exhausted for this agent")
    agent = KyokTokenAgent()
    served, stream = await _run_with_token(souk, serve, agent, ref)

    body = _completion_body()
    relay = await KyokAdapter(souk).complete(
        agent.token, body, **_signed_call(served.identity, agent.token, body)
    )
    with pytest.raises(KyokRejected) as exc:
        await relay.collapsed()
    assert exc.value.status == 502
    assert "quota" in str(exc.value)
    await _finish(agent, stream)


async def test_a_detached_llm_provider_is_a_503_not_a_hang(souk, serve, llm):
    _, identity, ref = llm
    agent = KyokTokenAgent()
    served, stream = await _run_with_token(souk, serve, agent, ref)

    souk.detach_llm_provider(identity.public_key)
    body = _completion_body()
    with pytest.raises(KyokRejected) as exc:
        await KyokAdapter(souk).complete(
            agent.token, body, **_signed_call(served.identity, agent.token, body)
        )
    assert exc.value.status == 503
    await _finish(agent, stream)


async def test_a_finished_run_stops_spending_at_once(souk, serve, llm):
    _, _, ref = llm
    agent = KyokTokenAgent()
    served, stream = await _run_with_token(souk, serve, agent, ref)
    token = agent.token
    await _finish(agent, stream)

    body = _completion_body()
    with pytest.raises(KyokRejected) as exc:
        await KyokAdapter(souk).complete(
            token, body, **_signed_call(served.identity, token, body)
        )
    assert exc.value.status == 403


async def test_the_binding_dies_with_the_run(souk, serve, llm):
    _, _, ref = llm
    agent = KyokTokenAgent()
    _, stream = await _run_with_token(souk, serve, agent, ref)
    assert souk.kyok_relay.binding_for(agent.run_id).llm_provider == ref
    await _finish(agent, stream)
    assert souk.kyok_relay.binding_for(agent.run_id) is None


async def test_an_unknown_llm_offering_fails_the_run_at_start(souk, serve, llm):
    _, identity, _ = llm
    agent = KyokTokenAgent()
    served = await serve(agent, "kyok-agent")
    wrong = LlmRef(provider_key=identity.public_key, name="no-such-model")
    with pytest.raises(LlmProviderNotFound, match="no-such-model"):
        await AGUIAdapter(souk).run(served.agents["kyok-agent"], _body(wrong))


async def test_a_run_without_the_opt_in_gets_no_token(souk, serve):
    agent = KyokTokenAgent()
    served = await serve(agent, "kyok-agent")
    stream = await AGUIAdapter(souk).run(served.agents["kyok-agent"], _body(None))
    await asyncio.wait_for(agent.got_token.wait(), timeout=5)
    assert agent.token is None
    await _finish(agent, stream)


async def test_two_providers_may_both_offer_gpt4(souk, serve, llm):
    stub_a, _, ref_a = llm
    other = ProviderIdentity.generate()
    signature, timestamp = sign_llm_registration(other, ["gpt4"])
    await souk.register_llm_providers(other.public_key, signature, timestamp, ["gpt4"])
    stub_b = StubLLM(answer="other answer")
    await souk.attach_llm_provider(InProcessLLMProvider(other, stub_b), ["gpt4"])
    try:
        agent = KyokTokenAgent()
        served, stream = await _run_with_token(
            souk, serve, agent, LlmRef(provider_key=other.public_key, name="gpt4")
        )
        body = _completion_body()
        completion = await (
            await KyokAdapter(souk).complete(
                agent.token, body, **_signed_call(served.identity, agent.token, body)
            )
        ).collapsed()

        assert completion.choices[0].message.content == "other answer"
        assert stub_b.seen and not stub_a.seen
        await _finish(agent, stream)
    finally:
        souk.detach_llm_provider(other.public_key)


async def test_attach_touches_and_announces_like_attach_provider(souk):
    from souk.changes import LlmRosterChanged

    events: list = []
    unsubscribe = souk.on_change(events.append)
    try:
        identity = ProviderIdentity.generate()
        signature, timestamp = sign_llm_registration(identity, ["m"])
        await souk.register_llm_providers(identity.public_key, signature, timestamp, ["m"])
        async with souk.session() as session:
            before = (await repo.get_llm_provider(
                session, LlmRef(provider_key=identity.public_key, name="m")
            ))["last_seen_at"]

        await souk.attach_llm_provider(InProcessLLMProvider(identity, StubLLM()), ["m"])
        async with souk.session() as session:
            after = (await repo.get_llm_provider(
                session, LlmRef(provider_key=identity.public_key, name="m")
            ))["last_seen_at"]
        assert after >= before

        souk.detach_llm_provider(identity.public_key)
        souk.detach_llm_provider(identity.public_key)
        assert [type(e) for e in events].count(LlmRosterChanged) == 3
    finally:
        unsubscribe()


async def test_an_unregistered_llm_provider_cannot_attach(souk):
    lurker = InProcessLLMProvider(ProviderIdentity.generate(), StubLLM())
    with pytest.raises(LlmProviderNotFound):
        await souk.attach_llm_provider(lurker, ["gpt4"])
