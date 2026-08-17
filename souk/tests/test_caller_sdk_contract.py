"""The two sides state the same interaction independently; this checks they
still agree.

`souk_caller_sdk` cannot import souk, and names nothing of souk's either — a
completion arrives as its own `PendingCompletion` and answers leave through a
`CallerLink`. So nothing makes the pair agree by construction, and the seam is
`souk_caller_sdk.InProcessLink`: the one module there that speaks souk's words,
and still does not import it.

That seam is what these assert. Its sibling has been broken before, which is
why it is worth the file: souk delivered its own dispatch object, whose input
field is `input_json`, to a provider reading `run_input`, and the run died on
its first event with nothing red anywhere.

`test_kyok_in_process.py` exercises the same seam end to end. These are the
cheap, specific failures — which name moved — that a behavioural test reports
only as "it stopped working".
"""

from __future__ import annotations

import inspect

from souk_caller_sdk import (
    CallerLink,
    InProcessLink,
    LINK_METHODS,
    PENDING_COMPLETION_FIELDS,
    PendingCompletion,
    RUN_METADATA_FIELDS,
    RUN_METADATA_KEY,
    run_metadata,
)

from souk.models import AgentRef
from souk.protocols.agui import build_forwarded_props
from souk.protocols.kyok import KyokAdapter

_AGENT = AgentRef(provider_key="ab" * 32, name="translator")


def test_the_port_the_sdk_declares_is_the_port_it_supplies():
    for name, params in LINK_METHODS.items():
        method = getattr(CallerLink, name, None)
        assert method is not None, f"CallerLink has no {name}"
        assert inspect.iscoroutinefunction(method)
        bound = [p for p in inspect.signature(method).parameters if p != "self"]
        assert bound == list(params), f"{name}{params} vs {bound}"


def test_the_completion_the_sdk_declares_is_the_one_it_hands_over():
    """Compared as data rather than by building one, so a field added to
    `PendingCompletion` that `InProcessLink` never fills fails here instead of
    arriving empty at somebody's bridge."""
    assert set(PendingCompletion.__dataclass_fields__) == PENDING_COMPLETION_FIELDS

    source = inspect.getsource(InProcessLink.claim)
    for field in PENDING_COMPLETION_FIELDS:
        assert f"{field}=" in source, f"the adapter never fills {field}"


def test_souk_still_has_the_two_methods_the_adapter_calls():
    """The seam, from souk's side. `InProcessLink` calls `poll` and `respond`
    by name on whatever it was given; a rename in core has to fail here."""
    for name, arity in (("poll", 2), ("respond", 2)):
        method = getattr(KyokAdapter, name, None)
        assert method is not None, f"KyokAdapter has no {name}"
        assert inspect.iscoroutinefunction(method)
        bound = [p for p in inspect.signature(method).parameters if p != "self"]
        assert len(bound) == arity, f"{name}: {bound}"


def test_the_adapter_reads_the_keys_souks_poll_actually_answers_with():
    """`poll` answers in souk's spelling — `requestId`, `body` — and
    `InProcessLink` is the only place that turns those into the SDK's. Both
    halves are written out here rather than derived from either side.
    """
    source = inspect.getsource(InProcessLink.claim)
    assert 'queued["requestId"]' in source
    assert 'queued["body"]' in source

    answers = inspect.getsource(KyokAdapter.poll)
    # souk's own answer comes from KyokBridge.poll_one; the adapter just
    # forwards it, so the shape is asserted where it is produced.
    assert "poll_one" in answers


def test_the_metadata_key_the_sdk_writes_is_the_one_souk_reads():
    """A caller offers KYOK by putting exactly this in a run's metadata, and
    souk mints a token only when it is there. Two independent statements of
    one string, compared."""
    assert set(run_metadata("sess_1")[RUN_METADATA_KEY]) == RUN_METADATA_FIELDS

    props = build_forwarded_props(
        "test-signing-secret", "run_1", _AGENT, run_metadata("sess_1"), None
    )

    assert "kyok" in props and props["kyok"]["token"]


def test_a_run_without_that_key_is_simply_not_offering_kyok():
    """The other half of opt-in: no metadata, no token, and a provider that
    never sees one calls its own configured LLM as always."""
    assert build_forwarded_props("test-signing-secret", "run_1", _AGENT, {}, None) is None
