from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from openai.types.chat import ChatCompletionChunk

from souk_llm_provider_sdk import (
    DELIVERED_COMPLETION_FIELDS,
    DeliveredCompletion,
    InProcessLLMProvider,
    ProviderIdentity,
    llm_registration_payload,
    sign_llm_registration,
)


def test_delivered_completion_fields_match_contract():
    assert set(DeliveredCompletion.model_fields) == DELIVERED_COMPLETION_FIELDS


def test_llm_registration_payload_states_its_operation_and_sorts_names():
    payload = llm_registration_payload(["smart", "fast"], 1755300000).decode()
    assert payload == "souk-register-llm:fast,smart:1755300000"


def test_sign_llm_registration_returns_signature_over_its_own_timestamp():
    identity = ProviderIdentity.generate()
    signature, timestamp = sign_llm_registration(identity, ["gpt4"], 1755300000)
    assert timestamp == 1755300000
    assert isinstance(signature, str) and len(signature) == 128


@dataclass(frozen=True)
class _AgentRefLike:
    provider_key: str
    name: str


@dataclass(frozen=True)
class _CompletionRequestLike:
    run_id: str
    agent: _AgentRefLike
    body: dict
    llm_name: str = "gpt4"
    context: dict | None = None
    actor_chain: list | None = None


async def test_inprocess_provider_hands_its_llm_a_delivered_completion():
    seen: list[DeliveredCompletion] = []

    async def llm(delivered: DeliveredCompletion) -> AsyncIterator[ChatCompletionChunk]:
        seen.append(delivered)
        return
        yield  # pragma: no cover — makes this an async generator

    provider = InProcessLLMProvider(ProviderIdentity.generate(), llm)
    request = _CompletionRequestLike(
        run_id="run_1",
        agent=_AgentRefLike(provider_key="ab" * 32, name="translator"),
        body={"model": "m", "messages": []},
        context={"voucher": "v1"},
        actor_chain=["jwt1", "jwt2"],
    )
    async for _ in provider.complete(request):
        pass

    assert seen == [
        DeliveredCompletion(
            run_id="run_1",
            provider_key="ab" * 32,
            agent_name="translator",
            body={"model": "m", "messages": []},
            llm_name="gpt4",
            context={"voucher": "v1"},
            actor_chain=["jwt1", "jwt2"],
        )
    ]


def test_public_key_is_the_identitys_own():
    identity = ProviderIdentity.generate()
    assert InProcessLLMProvider(identity, llm=None).public_key == identity.public_key


async def test_any_link_gets_the_same_translation_the_in_process_one_does():
    from souk_llm_provider_sdk import SoukLLMLink

    seen: list[DeliveredCompletion] = []

    class RecordingLink(SoukLLMLink):
        @property
        def public_key(self) -> str:
            return "abc123"

        def serve(self, delivered: DeliveredCompletion) -> AsyncIterator[ChatCompletionChunk]:
            seen.append(delivered)

            async def _nothing():
                return
                yield

            return _nothing()

    request = _CompletionRequestLike(
        run_id="r-1",
        agent=_AgentRefLike(provider_key="k", name="a"),
        body={"model": "m", "messages": []},
        context={"voucher": "v"},
        actor_chain=["hop0"],
    )
    async for _ in RecordingLink().complete(request):
        pass

    (delivered,) = seen
    assert (delivered.run_id, delivered.provider_key, delivered.agent_name) == ("r-1", "k", "a")
    assert (delivered.context, delivered.actor_chain) == ({"voucher": "v"}, ["hop0"])


async def test_the_readme_ceiling_pattern_refuses_structurally():
    from souk_llm_provider_sdk import CompletionRefused

    spent: dict[str, int] = {}

    async def guarded(delivered: DeliveredCompletion):
        spent[delivered.run_id] = spent.get(delivered.run_id, 0) + 1
        if spent[delivered.run_id] > 2:
            raise CompletionRefused({"kind": "budget-ceiling", "runId": delivered.run_id})
        return
        yield

    provider = InProcessLLMProvider(ProviderIdentity.generate(), guarded)
    request = _CompletionRequestLike(
        run_id="r-capped", agent=_AgentRefLike(provider_key="k", name="a"), body={}
    )
    for _ in range(2):
        async for _ in provider.complete(request):
            pass
    try:
        async for _ in provider.complete(request):
            pass
        raise AssertionError("third completion was served past the ceiling")
    except CompletionRefused as e:
        assert e.refusal == {"kind": "budget-ceiling", "runId": "r-capped"}
