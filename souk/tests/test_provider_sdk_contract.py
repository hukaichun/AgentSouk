"""The two sides state the same interaction independently; this checks they
still agree.

`souk_provider_sdk` cannot import souk, so nothing makes the pair agree by
construction. It has been broken: souk delivered its own dispatch object,
whose input field is `input_json`, to a provider reading `run_input`, and the
run died on its first event with nothing red anywhere.

Both directions are checked, because both cross the boundary — souk hands a
run over, and the provider hands events back.
"""

from __future__ import annotations

import inspect

from souk_provider_sdk import (
    AGENT_FIELDS,
    CLAIMED_RUN_FIELDS,
    HandleProvider,
    ProviderIdentity,
    ProviderRuntime,
)

from souk.broker import ConnectedProvider
from souk.models import AgentRef, ClaimedRun


def test_a_delivered_run_carries_exactly_what_a_provider_reads():
    assert set(ClaimedRun.model_fields) == CLAIMED_RUN_FIELDS


def test_the_agent_on_a_delivered_run_is_still_the_pair():
    assert set(AgentRef.model_fields) == AGENT_FIELDS


def test_the_sdk_runtime_is_something_souks_broker_can_deliver_to():
    # An instance, not the class: `max_concurrent_runs` is set in __init__,
    # which is exactly the kind of thing a class-level hasattr misses.
    runtime = ProviderRuntime(ProviderIdentity.generate(), HandleProvider([]), souk=None)

    for name in ConnectedProvider.__annotations__:
        assert hasattr(runtime, name), f"ConnectedProvider needs {name}"
    assert inspect.iscoroutinefunction(runtime.deliver)
    assert not inspect.iscoroutinefunction(runtime.cancel)


def test_souk_satisfies_the_connection_the_provider_reports_back_through():
    from souk.core import Souk

    # Synchronous on purpose: a provider must never wait on souk's
    # persistence, and the end-of-stream marker is sent while unwinding a
    # cancellation, where an await would be interrupted before arriving.
    assert not inspect.iscoroutinefunction(Souk.report_event)
    assert not inspect.iscoroutinefunction(Souk.finish_run)
