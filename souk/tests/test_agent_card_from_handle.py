"""What a provider declares about its agent reaches souk.

The contract test beside this one compares the two field lists. This one
*registers* through a handle and reads the card back, which is what actually
failed: `AgentHandle` had no `agent_card_extra`, `register_agents` defaults it
with `.get(..., {})`, and so every SDK-registered agent got a card of name and
description alone. No error, a normal-looking roster entry, and an agent
nothing could search for.

Skills are the reason this matters rather than a detail: `agent_card_extra` is
the only route they take into souk, and skills and their tags are what
discovery searches on. An agent with none is reachable only by somebody who
already knows its name — which, in a market, is the whole thing not working.
"""

from __future__ import annotations

import pytest
from souk_provider_sdk import AgentHandle

SKILLS = [
    {"id": "translate", "name": "Translate", "tags": ["language", "text"]},
    {"id": "summarize", "name": "Summarize", "tags": ["text"]},
]


async def _run_stream(run_input: dict):
    yield {"type": "RUN_FINISHED", "threadId": run_input.thread_id, "runId": run_input.run_id}


async def _register(souk, identity, *handles: AgentHandle):
    signature, timestamp = identity.sign_registration([h.name for h in handles])
    return await souk.register_agents(
        identity.public_key, signature, timestamp, [h.as_registration() for h in handles]
    )


async def test_skills_declared_on_a_handle_reach_the_roster(souk, new_identity):
    identity = new_identity()
    handle = AgentHandle(
        name="translator",
        run_stream=_run_stream,
        description="translates things",
        agent_card_extra={"skills": SKILLS},
    )

    await _register(souk, identity, handle)

    listed = next(a for a in await souk.list_agents() if a.name == "translator")
    assert listed.skills == SKILLS


async def test_the_card_keeps_name_and_description_alongside_the_extra(souk, new_identity):
    """`agent_card_extra` is merged into the card, not substituted for it —
    so a handle that sets it must not lose the two fields that were always
    there."""
    identity = new_identity()
    handle = AgentHandle(
        name="translator",
        run_stream=_run_stream,
        description="translates things",
        agent_card_extra={"skills": SKILLS, "version": "2.0"},
    )

    registration = await _register(souk, identity, handle)

    card = (await souk.get_agent(registration.agents["translator"])).agent_card
    assert card["name"] == "translator"
    assert card["description"] == "translates things"
    assert card["version"] == "2.0"
    assert card["skills"] == SKILLS


async def test_metadata_is_stored_and_stays_off_the_public_card(souk, new_identity):
    """souk-internal by design — the A2A Agent Card is a public statement and
    `metadata` is not part of it."""
    identity = new_identity()
    handle = AgentHandle(
        name="translator",
        run_stream=_run_stream,
        metadata={"cost_centre": "research", "owner": "ada"},
    )

    registration = await _register(souk, identity, handle)
    record = await souk.get_agent(registration.agents["translator"])

    assert record.metadata == {"cost_centre": "research", "owner": "ada"}
    assert "cost_centre" not in record.agent_card


async def test_a_handle_that_declares_nothing_extra_still_registers(souk, new_identity):
    """The common case has to stay unchanged: the two new fields default
    empty and are omitted from the batch entirely."""
    identity = new_identity()
    handle = AgentHandle(name="plain", run_stream=_run_stream, description="d")

    assert handle.as_registration() == {"name": "plain", "description": "d"}

    registration = await _register(souk, identity, handle)
    record = await souk.get_agent(registration.agents["plain"])

    assert record.agent_card == {"name": "plain", "description": "d"}
    assert record.metadata == {}


@pytest.mark.parametrize("field_name", ["agent_card_extra", "metadata"])
async def test_re_registering_replaces_what_the_handle_now_says(souk, new_identity, field_name):
    """Registration is declarative: a handle that drops a skill has dropped
    it, rather than leaving the old card merged underneath."""
    identity = new_identity()

    await _register(
        souk,
        identity,
        AgentHandle(name="a", run_stream=_run_stream, **{field_name: {"skills": SKILLS}}),
    )
    registration = await _register(
        souk,
        identity,
        AgentHandle(name="a", run_stream=_run_stream, **{field_name: {"skills": SKILLS[:1]}}),
    )

    record = await souk.get_agent(registration.agents["a"])
    stored = record.agent_card if field_name == "agent_card_extra" else record.metadata
    assert stored["skills"] == SKILLS[:1]
