"""The two sides state the same interaction independently; this checks they
still agree.

`souk_provider_sdk` cannot import souk, and no longer names anything of
souk's either — runs reach it as its own `DeliveredRun` and results leave
through two callables. So nothing makes the pair agree by construction, and
the seam has moved: it is now `souk_provider_sdk.InProcessProvider`,
the one module there that names souk's shapes — and still does not import it.

That seam is what these assert. It has been broken before: souk delivered its
own dispatch object, whose input field is `input_json`, to a provider reading
`run_input`, and the run died on its first event with nothing red anywhere.
"""

from __future__ import annotations

import inspect
import re

from souk_provider_sdk import (
    CONNECTED_PROVIDER_ATTRS,
    DELIVERED_RUN_FIELDS,
    REGISTRATION_FIELDS,
    REPORT_CALLBACKS,
    InProcessProvider,
    AgentHandle,
    DeliveredRun,
    HandleProvider,
    ProviderIdentity,
    ProviderRuntime,
)

from souk import repo
from souk.broker import ConnectedProvider
from souk.models import ClaimedRun



def test_the_adapter_can_fill_every_field_the_sdk_declares():
    """Whatever souk hands over must be enough to build the SDK's own type.

    The fields are compared as data rather than by building one, so a field
    added to `DeliveredRun` that the adapter never fills fails here instead
    of arriving empty at somebody's agent.
    """
    assert set(DeliveredRun.__dataclass_fields__) == DELIVERED_RUN_FIELDS

    source = inspect.getsource(InProcessProvider.deliver)
    for field in ("run_id", "agent_name", "run_input", "thread_id"):
        assert f"{field}=" in source, f"the adapter never fills {field}"


def test_what_souk_delivers_still_carries_what_the_adapter_reads():
    """The other direction: the adapter reads these off souk's `ClaimedRun`,
    so a rename there has to fail here rather than at the first run."""
    assert {"run_id", "agent", "thread_id", "run_input"} <= set(ClaimedRun.model_fields)


def test_the_two_sides_agree_on_what_a_connected_provider_is():
    """Equality, not containment: a fifth member added to souk's protocol has
    to fail here.

    `__annotations__` was the old check and would not have caught one — it
    holds `public_key` and `max_concurrent_runs` but neither method, so a new
    *method* on the protocol was invisible to it. `__protocol_attrs__` is the
    whole membership.
    """
    assert ConnectedProvider.__protocol_attrs__ == set(CONNECTED_PROVIDER_ATTRS)


def test_the_in_process_connection_is_something_souks_broker_can_deliver_to():
    runtime = ProviderRuntime(ProviderIdentity.generate(), HandleProvider([]))
    adapter = InProcessProvider(souk=None, runtime=runtime)

    for name in ConnectedProvider.__protocol_attrs__:
        assert hasattr(adapter, name), f"ConnectedProvider needs {name}"
    assert inspect.iscoroutinefunction(adapter.deliver)
    assert not inspect.iscoroutinefunction(adapter.cancel)


def test_the_runtime_itself_needs_the_same_trio():
    """Stated by the SDK, checked against the SDK: `CONNECTED_PROVIDER_ATTRS`
    is what it says a caller needs, and it is what the runtime has."""
    runtime = ProviderRuntime(ProviderIdentity.generate(), HandleProvider([]))

    for name in CONNECTED_PROVIDER_ATTRS:
        assert hasattr(runtime, name)


def test_the_callbacks_the_sdk_declares_are_what_the_adapter_supplies():
    runtime = ProviderRuntime(ProviderIdentity.generate(), HandleProvider([]))
    InProcessProvider(souk=None, runtime=runtime)

    for name, params in REPORT_CALLBACKS.items():
        callback = getattr(runtime, name)
        assert callback is not None, f"the adapter left {name} unset"
        # Synchronous on purpose: an agent must never wait on souk's
        # persistence, and on_finish is called while unwinding a
        # cancellation, where an await would be interrupted before arriving.
        assert not inspect.iscoroutinefunction(callback)
        bound = list(inspect.signature(callback).parameters)
        assert len(bound) == len(params), f"{name}{params} vs {bound}"


def test_souk_still_has_the_two_calls_the_adapter_reports_through():
    from souk.core import Souk

    assert not inspect.iscoroutinefunction(Souk.report_event)
    assert not inspect.iscoroutinefunction(Souk.finish_run)
    for method in (Souk.report_event, Souk.finish_run):
        assert "claimed_by" in inspect.signature(method).parameters


def test_a_handle_can_express_everything_register_agents_reads():
    """The two sides of registration, compared — which nothing did before.

    souk has read `agent_card_extra` and `metadata` since its first commit.
    The HTTP model that filled them left with the serving layer, and the
    `AgentHandle` written to replace it had neither, so every agent
    registered through the SDK got a card of name and description alone: no
    skills, therefore invisible to discovery, and indistinguishable from
    healthy because `register_agents` defaults both keys.

    Read off the source rather than a list repeated here — a fifth key added
    to `register_agents` has to fail this, and it cannot if the expectation
    is written down twice.
    """
    source = inspect.getsource(repo.register_agents)
    read_by_souk = set(re.findall(r"agent(?:\.get\(|\[)[\"']([a-z_]+)[\"']", source))

    assert read_by_souk, "no keys found — the regex stopped matching, not a passing test"
    assert read_by_souk == set(REGISTRATION_FIELDS), (
        f"register_agents reads {sorted(read_by_souk)}, "
        f"the SDK declares {sorted(REGISTRATION_FIELDS)}"
    )


def test_the_handle_actually_carries_those_fields():
    """`REGISTRATION_FIELDS` is a claim about `AgentHandle`; this is the
    claim being true. Declaring a field souk reads and not having it is the
    same defect one indirection later."""
    declared = set(AgentHandle.__dataclass_fields__)

    assert REGISTRATION_FIELDS <= declared, (
        f"AgentHandle cannot express {sorted(REGISTRATION_FIELDS - declared)}"
    )
