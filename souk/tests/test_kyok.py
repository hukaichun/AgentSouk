from __future__ import annotations

import base64
import json

import pytest

from openai.types.chat import ChatCompletionChunk

from souk.identity import kyok_call_signing_payload, llm_registration_signing_payload
from souk_provider_sdk.identity import kyok_call_payload
from souk_llm_provider_sdk import llm_registration_payload
from souk.models import AgentRef, LlmRef
from souk.protocols.kyok import collapse_stream
from souk.kyok import (
    KyokBinding,
    KyokRelay,
    issue_kyok_token,
    kyok_forwarded_props,
    parse_kyok_opt_in,
    read_kyok_forwarded_props,
    strip_kyok_context,
    verify_kyok_token,
)


_AGENT = AgentRef(provider_key="ab" * 32, name="translator")


def test_a_kyok_call_signs_what_it_is_for():
    payload = kyok_call_signing_payload("some-token", 1755300000, "ab" * 32).decode()

    assert payload.startswith("souk-kyok-call:")
    assert payload == f"souk-kyok-call:some-token:1755300000:{'ab' * 32}"


def test_both_sides_state_the_kyok_signing_payload_the_same_way():
    assert kyok_call_payload("tok", 1755300000, "cafe") == kyok_call_signing_payload(
        "tok", 1755300000, "cafe"
    )


def test_both_sides_state_the_llm_registration_payload_the_same_way():
    assert llm_registration_payload(["smart", "fast"], 1755300000) == (
        llm_registration_signing_payload(["fast", "smart"], 1755300000)
    )


def test_kyok_token_roundtrip():
    token = issue_kyok_token("run_1", _AGENT, "test-signing-secret")
    result = verify_kyok_token(token, "test-signing-secret")
    assert result is not None
    assert result.run_id == "run_1"
    assert result.agent == _AGENT


def test_a_token_carries_exactly_the_run_the_agent_and_its_expiry():
    token = issue_kyok_token("run_1", _AGENT, "test-signing-secret")
    body = json.loads(base64.urlsafe_b64decode(token.split(".", 1)[0].encode()))

    assert set(body) == {"runId", "providerKey", "agentName", "exp"}
    assert body["runId"] == "run_1"
    assert body["providerKey"] == _AGENT.provider_key
    assert body["agentName"] == _AGENT.name


def test_expired_kyok_token_rejected(monkeypatch):
    import souk.kyok as kyok_module

    monkeypatch.setattr(kyok_module, "KYOK_TOKEN_TTL_SECONDS", -1)
    token = kyok_module.issue_kyok_token("run_1", _AGENT, "test-signing-secret")
    assert verify_kyok_token(token, "test-signing-secret") is None


def test_tampered_kyok_token_signature_rejected():
    token = issue_kyok_token("run_1", _AGENT, "test-signing-secret")
    body, signature = token.split(".", 1)
    tampered = f"{body}.{'0' * len(signature)}"
    assert verify_kyok_token(tampered, "test-signing-secret") is None


@pytest.mark.parametrize(
    "malformed",
    ["not-a-token-at-all", "onlyonepart", "bm90anNvbg==.deadbeef"],
)
def test_malformed_kyok_token_rejected(malformed):
    assert verify_kyok_token(malformed, "test-signing-secret") is None


def test_a_well_formed_opt_in_parses_to_the_pair_and_context():
    opt_in = parse_kyok_opt_in(
        {"kyok": {"llmProvider": {"providerKey": "ab" * 32, "name": "gpt4"}, "context": {"v": 1}}}
    )
    assert opt_in.llm_provider == LlmRef(provider_key="ab" * 32, name="gpt4")
    assert opt_in.context == {"v": 1}


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"kyok": "not-a-dict"},
        {"kyok": {"llmProvider": "bare-name-is-not-an-address"}},
        {"kyok": {"llmProvider": {"name": "gpt4"}}},
    ],
)
def test_anything_else_is_no_opt_in_not_an_error(metadata):
    opt_in = parse_kyok_opt_in(metadata)
    assert opt_in is None or opt_in.llm_provider is None


def test_strip_removes_exactly_the_context():
    metadata = {"kyok": {"llmProvider": {"providerKey": "k", "name": "m"}, "context": "secret"}, "other": 1}
    stripped = strip_kyok_context(metadata)
    assert "context" not in stripped["kyok"]
    assert stripped["kyok"]["llmProvider"] == {"providerKey": "k", "name": "m"}
    assert stripped["other"] == 1
    assert metadata["kyok"]["context"] == "secret"


def test_forwarded_props_roundtrip_through_the_model():
    entry = kyok_forwarded_props("run_1", _AGENT, "test-signing-secret")
    grant = read_kyok_forwarded_props({"kyok": entry})
    assert grant is not None
    decoded = verify_kyok_token(grant.token, "test-signing-secret")
    assert decoded.run_id == "run_1" and decoded.agent == _AGENT
    assert read_kyok_forwarded_props({}) is None
    assert read_kyok_forwarded_props(None) is None


def _chunk(
    deltas: list[tuple[int, str]],
    finish: list[tuple[int, str]] | None = None,
    role: bool = False,
) -> ChatCompletionChunk:
    choices = []
    for index, content in deltas:
        delta: dict = {"content": content}
        if role:
            delta["role"] = "assistant"
        choices.append({"index": index, "delta": delta, "finish_reason": None})
    for index, reason in finish or []:
        choices.append({"index": index, "delta": {}, "finish_reason": reason})
    return ChatCompletionChunk.model_validate(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1755300000,
            "model": "test-model",
            "choices": choices,
        }
    )


def test_collapse_of_no_chunks_is_an_empty_completion():
    completion = collapse_stream([])
    assert completion.choices[0].message.content == ""
    assert completion.choices[0].finish_reason == "stop"


def test_collapse_of_one_chunk():
    completion = collapse_stream([_chunk([(0, "hello")], finish=[(0, "stop")], role=True)])
    assert completion.object == "chat.completion"
    assert completion.model == "test-model"
    assert completion.choices[0].message.content == "hello"
    assert completion.choices[0].message.role == "assistant"
    assert completion.choices[0].finish_reason == "stop"


def test_collapse_concatenates_deltas_in_order():
    completion = collapse_stream(
        [
            _chunk([(0, "hel")], role=True),
            _chunk([(0, "lo ")]),
            _chunk([(0, "world")], finish=[(0, "stop")]),
        ]
    )
    assert completion.choices[0].message.content == "hello world"
    assert completion.choices[0].finish_reason == "stop"


def test_collapse_keeps_choices_apart_by_index():
    completion = collapse_stream(
        [
            _chunk([(0, "a"), (1, "x")], role=True),
            _chunk([(1, "y"), (0, "b")], finish=[(0, "stop"), (1, "length")]),
        ]
    )
    assert [c.index for c in completion.choices] == [0, 1]
    assert completion.choices[0].message.content == "ab"
    assert completion.choices[1].message.content == "xy"
    assert completion.choices[1].finish_reason == "length"


class _Link:
    public_key = "cd" * 32

    def complete(self, request):  # pragma: no cover — never called here
        raise AssertionError


_GPT4 = LlmRef(provider_key=_Link.public_key, name="gpt4")


def test_a_binding_lives_until_its_run_is_discarded():
    relay = KyokRelay()
    relay.bind_run("run_1", KyokBinding(llm_provider=_GPT4))
    assert relay.binding_for("run_1").llm_provider == _GPT4
    relay.discard("run_1")
    assert relay.binding_for("run_1") is None
    assert relay._bindings == {}


def test_discard_of_a_run_that_never_bound_is_a_no_op():
    KyokRelay().discard("never-seen")


def test_inherit_copies_offering_and_context_with_the_childs_own_chain():
    relay = KyokRelay()
    relay.bind_run(
        "run_parent",
        KyokBinding(llm_provider=_GPT4, context={"voucher": "v1"}, actor_chain=["hop1"]),
    )

    assert relay.inherit("run_parent", "run_child", ["hop1", "hop2"]) is True
    child = relay.binding_for("run_child")
    assert child.llm_provider == _GPT4
    assert child.context == {"voucher": "v1"}
    assert child.actor_chain == ["hop1", "hop2"]

    assert relay.inherit("never-bound", "run_x", None) is False
    assert relay.binding_for("run_x") is None


def test_detach_removes_every_offering_of_that_identity():
    relay = KyokRelay()
    link = _Link()
    fast = LlmRef(provider_key=link.public_key, name="fast")
    relay.attach({_GPT4: link, fast: link})
    assert relay.serving(_GPT4) is link
    relay.detach(link.public_key)
    assert relay.serving(_GPT4) is None
    assert relay.serving(fast) is None
    assert relay._links == {}


def test_reattach_replaces_the_connection_under_the_same_offering():
    relay = KyokRelay()
    old, new = _Link(), _Link()
    relay.attach({_GPT4: old})
    relay.attach({_GPT4: new})
    assert relay.serving(_GPT4) is new


def test_broker_forget_funnel_discards_the_binding():
    from souk.broker import RunBroker

    relay = KyokRelay()
    broker = RunBroker()
    broker.add_forget_listener(relay.discard)
    relay.bind_run("run_1", KyokBinding(llm_provider=_GPT4))
    broker.forget("run_1")
    assert relay.binding_for("run_1") is None
