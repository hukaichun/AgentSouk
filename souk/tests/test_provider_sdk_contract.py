"""souk still provides what a provider SDK reads off it.

A provider cannot import souk, so the two agree by duck typing: a worker reads
fields off whatever `claim_work` returns and recognises souk's refusals by the
name of the exception class. Duck typing across a repository boundary is a
promise nobody holds — this file is what holds it.

The SDK states its expectations as data (`souk_provider_sdk.contract`) and
these assert souk still meets them. Same device as the signing payloads, and
for the same reason: a payload was changed here while every test signed with
souk's own builder, 219 tests passed, and no provider in the world could
register. A contract only catches anything when the two sides are stated
independently.

If one of these fails, the fix is *not* to update the frozenset until it
passes. It is to decide whether souk meant to change its contract — and if it
did, to say so downstream, because nothing else will.
"""

from __future__ import annotations

from dataclasses import fields

from souk_provider_sdk.contract import (
    AGENT_FIELDS,
    CLAIMED_RUN_FIELDS,
    RECOGNISED_SOUK_ERRORS,
)

from souk.errors import InvalidRegistration, NothingOwned
from souk.models import AgentRef
from souk.worker import ClaimedRun


def test_a_claimed_run_carries_exactly_what_a_worker_reads():
    """Exactly, not merely enough. A field souk hands over that no worker
    reads is one somebody meant a provider to have and nothing gives it."""
    assert {f.name for f in fields(ClaimedRun)} == CLAIMED_RUN_FIELDS


def test_the_agent_on_a_claimed_run_is_still_the_pair():
    """The loop routes on `run.agent.name`. An agent is
    `(provider_key, name)` and has no other identity."""
    assert set(AgentRef.model_fields) == AGENT_FIELDS


def test_the_refusals_a_worker_acts_on_still_have_the_names_it_matches():
    """The nastiest of the three if it breaks, because it breaks *quietly*.

    A worker matches on `type(exc).__name__`. Rename `NothingOwned` and the
    match silently stops happening — no crash, no error, just a fall-through
    to "unknown failure, retry". The provider goes back to spinning forever
    with clean logs, which is issue #37 restored, and nothing anywhere turns
    red. So the names are asserted rather than trusted.
    """
    assert NothingOwned.__name__ in RECOGNISED_SOUK_ERRORS
    assert InvalidRegistration.__name__ in RECOGNISED_SOUK_ERRORS


def test_souk_satisfies_the_connection_the_worker_expects():
    """The three methods, by name and by being callable. A `Souk` is passed
    straight to a worker in-process, so this is the whole interface between
    them — and `claim_work` is async while the other two are deliberately not
    (a worker reporting an event must never wait on souk's persistence, and
    `finish_run` runs while unwinding a cancellation, where an await would be
    interrupted before it ever arrived).
    """
    import inspect

    from souk.core import Souk

    assert inspect.iscoroutinefunction(Souk.claim_work)
    assert not inspect.iscoroutinefunction(Souk.report_event)
    assert not inspect.iscoroutinefunction(Souk.finish_run)
