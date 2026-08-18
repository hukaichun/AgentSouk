from __future__ import annotations

import inspect

from souk_llm_provider_sdk import (
    CONNECTED_LLM_PROVIDER_ATTRS,
    DELIVERED_COMPLETION_FIELDS,
    KYOK_FORWARDED_PROPS_KEY,
    InProcessLLMProvider,
    ProviderIdentity,
)
from souk_llm_provider_sdk.provider import DeliveredCompletion

from souk.kyok import CompletionRequest, ConnectedLLMProvider
from souk.protocols import agui as agui_module


def test_souk_asks_exactly_what_the_contract_says():
    assert ConnectedLLMProvider.__protocol_attrs__ == set(CONNECTED_LLM_PROVIDER_ATTRS)


def test_the_inprocess_provider_has_every_member_souk_asks_for():
    provider = InProcessLLMProvider(ProviderIdentity.generate(), llm=None)
    for attr in CONNECTED_LLM_PROVIDER_ATTRS:
        assert hasattr(provider, attr), attr
    assert callable(provider.complete)
    assert not inspect.iscoroutinefunction(provider.complete)


def test_the_adapter_reads_the_fields_souk_actually_sends():
    assert set(CompletionRequest.__dataclass_fields__) == {
        "run_id", "agent", "body", "llm_name", "context", "actor_chain",
    }
    assert set(DeliveredCompletion.__dataclass_fields__) == DELIVERED_COMPLETION_FIELDS

    source = inspect.getsource(InProcessLLMProvider.complete)
    for read in (
        "request.run_id",
        "request.agent.provider_key",
        "request.agent.name",
        "request.body",
        "request.llm_name",
        "request.context",
        "request.actor_chain",
    ):
        assert read in source, read


def test_the_token_travels_under_the_key_the_contract_names():
    assert KYOK_FORWARDED_PROPS_KEY == "kyok"
    source = inspect.getsource(agui_module.build_forwarded_props)
    assert f'extra["{KYOK_FORWARDED_PROPS_KEY}"]' in source
