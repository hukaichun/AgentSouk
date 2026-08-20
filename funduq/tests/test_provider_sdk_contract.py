from __future__ import annotations

import inspect
import re

import pytest

from funduq_provider_sdk import (
    CONNECTED_PROVIDER_ATTRS,
    Refusal,
    DELIVERED_RUN_FIELDS,
    REGISTRATION_FIELDS,
    LINK_QUERY_METHODS,
    LINK_REPORT_METHODS,
    InProcessLink,
    AgentHandle,
    DeliveredRun,
    HandleProvider,
    ProviderIdentity,
    ProviderRuntime,
)

from funduq import repo
from funduq.broker import ConnectedProvider, RunBroker
from funduq.models import ClaimedRun


def test_the_adapter_can_fill_every_field_the_sdk_declares():
    assert set(DeliveredRun.model_fields) == DELIVERED_RUN_FIELDS

    source = inspect.getsource(DeliveredRun.from_claimed)
    for field in ("run_id", "agent_name", "run_input", "thread_id"):
        assert f"{field}=" in source, f"the adapter never fills {field}"


def test_what_funduq_delivers_still_carries_what_the_adapter_reads():
    assert {"run_id", "agent", "thread_id", "run_input"} <= set(ClaimedRun.model_fields)


def test_the_two_sides_agree_on_what_a_connected_provider_is():
    assert ConnectedProvider.__protocol_attrs__ == set(CONNECTED_PROVIDER_ATTRS)


def test_the_in_process_connection_is_something_funduqs_broker_can_deliver_to():
    runtime = ProviderRuntime(ProviderIdentity.generate(), HandleProvider([]))
    adapter = InProcessLink(funduq=None, runtime=runtime)

    for name in ConnectedProvider.__protocol_attrs__:
        assert hasattr(adapter, name), f"ConnectedProvider needs {name}"
    assert inspect.iscoroutinefunction(adapter.deliver)
    assert not inspect.iscoroutinefunction(adapter.cancel)


def test_the_runtime_itself_needs_the_same_trio():
    runtime = ProviderRuntime(ProviderIdentity.generate(), HandleProvider([]))

    for name in CONNECTED_PROVIDER_ATTRS:
        assert hasattr(runtime, name)


def test_the_reporting_half_the_sdk_declares_is_what_the_link_supplies():
    link = InProcessLink(funduq=None, runtime=ProviderRuntime(ProviderIdentity.generate(), HandleProvider([])))

    for name, params in LINK_REPORT_METHODS.items():
        method = getattr(link, name, None)
        assert method is not None, f"the link has no {name}"
        assert inspect.iscoroutinefunction(method)
        bound = list(inspect.signature(method).parameters)
        assert len(bound) == len(params), f"{name}{params} vs {bound}"


def test_the_query_half_the_sdk_declares_is_what_the_link_supplies():
    link = InProcessLink(funduq=None, runtime=ProviderRuntime(ProviderIdentity.generate(), HandleProvider([])))

    for name, params in LINK_QUERY_METHODS.items():
        method = getattr(link, name, None)
        assert method is not None, f"the link has no {name}"
        assert inspect.iscoroutinefunction(method), f"{name} has to be awaitable — it crosses a wire"
        assert set(inspect.signature(method).parameters) == set(params)


def test_a_link_that_only_reports_is_not_constructible():
    from funduq_provider_sdk import FunduqLink

    class ReportsOnly(FunduqLink):
        public_key = "k"
        max_concurrent_runs = None

        async def offer(self, run):
            return True

        def cancel(self, run_id):
            pass

        async def report_event(self, run_id, event):
            pass

        async def finish_run(self, run_id):
            pass

    with pytest.raises(TypeError, match="thread_messages"):
        ReportsOnly()


def test_the_runtime_reports_through_the_link_and_holds_no_callbacks():
    runtime = ProviderRuntime(ProviderIdentity.generate(), HandleProvider([]))

    assert runtime.link is None
    assert not hasattr(runtime, "on_event")
    assert not hasattr(runtime, "on_finish")

    link = InProcessLink(funduq=None, runtime=runtime)
    assert runtime.link is link


def test_funduq_still_has_the_two_calls_the_adapter_reports_through():
    from funduq.core import Funduq

    assert not inspect.iscoroutinefunction(Funduq.report_event)
    assert not inspect.iscoroutinefunction(Funduq.finish_run)
    for method in (Funduq.report_event, Funduq.finish_run):
        assert "claimed_by" in inspect.signature(method).parameters


def test_a_handle_can_express_everything_register_agents_reads():
    source = inspect.getsource(repo.register_agents)
    read_by_funduq = set(re.findall(r"agent(?:\.get\(|\[)[\"']([a-z_]+)[\"']", source))

    assert read_by_funduq, "no keys found — the regex stopped matching, not a passing test"
    assert read_by_funduq == set(REGISTRATION_FIELDS), (
        f"register_agents reads {sorted(read_by_funduq)}, "
        f"the SDK declares {sorted(REGISTRATION_FIELDS)}"
    )


def test_the_handle_actually_carries_those_fields():
    declared = set(AgentHandle.__dataclass_fields__)

    assert REGISTRATION_FIELDS <= declared, (
        f"AgentHandle cannot express {sorted(REGISTRATION_FIELDS - declared)}"
    )


def test_a_refusal_is_read_by_the_attribute_the_sdk_declares():
    assert getattr(Refusal("gone"), "reason") == "gone"
    source = inspect.getsource(RunBroker._offer)
    assert 'getattr(accepted, "reason"' in source
