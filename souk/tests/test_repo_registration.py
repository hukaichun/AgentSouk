"""Covers the agent_id/soft-delist mechanics in souk.repo.register_agents —
the core of this session's identity rework: name is a non-unique display
label, agent_id (scoped to (public_key, name)) is the real ownership key,
and a registration batch is the declarative full statement of what an
identity currently offers (anything previously owned but omitted gets
soft-delisted).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from souk import repo
from souk.identity import provider_fingerprint
from souk.schema import agents, providers


async def _listed(session, souk):
    """repo.list_agents with this souk's own window policy — the settings
    it used to read from a global are now passed explicitly."""
    return await repo.list_agents(
        session,
        online_window_seconds=souk.settings.online_window_seconds,
        stale_hidden_window_seconds=souk.settings.stale_hidden_window_seconds,
    )


async def test_registration_assigns_and_reuses_agent_id(session, new_identity):
    identity = new_identity()
    agents = [{"name": "greeter", "description": "hi"}]

    first = await repo.register_agents(session, identity.public_key, agents)
    second = await repo.register_agents(session, identity.public_key, agents)

    assert first["greeter"] == second["greeter"]
    assert first["greeter"].startswith("agent_")


async def test_different_identity_same_name_gets_distinct_agent_id(session, new_identity):
    a = new_identity()
    b = new_identity()
    agents = [{"name": "greeter"}]

    result_a = await repo.register_agents(session, a.public_key, agents)
    result_b = await repo.register_agents(session, b.public_key, agents)

    assert result_a["greeter"] != result_b["greeter"]

    candidates = await repo.resolve_agents_by_name(session, "greeter")
    assert {c["agent_id"] for c in candidates} == {result_a["greeter"], result_b["greeter"]}


async def test_omitting_an_agent_soft_delists_it_and_reappearing_undoes_it(session, souk, new_identity):
    identity = new_identity()
    both = [{"name": "greeter"}, {"name": "translator"}]

    ids = await repo.register_agents(session, identity.public_key, both)
    translator_id = ids["translator"]

    # Re-register with translator omitted — the batch is the declarative
    # full statement of what this identity offers now.
    await repo.register_agents(session, identity.public_key, [{"name": "greeter"}])

    row = (
        await session.execute(
            select(agents.c.delisted_at).where(agents.c.agent_id == translator_id)
        )
    ).mappings().first()
    assert row["delisted_at"] is not None

    names_after_delist = [a["name"] for a in await _listed(session, souk)]
    assert names_after_delist == ["greeter"]

    # Reappearing in a later batch clears delisted_at again (self-heal).
    await repo.register_agents(session, identity.public_key, both)
    row = (
        await session.execute(
            select(agents.c.delisted_at).where(agents.c.agent_id == translator_id)
        )
    ).mappings().first()
    assert row["delisted_at"] is None
    names_after_return = sorted(a["name"] for a in await _listed(session, souk))
    assert names_after_return == ["greeter", "translator"]


async def test_list_agents_excludes_stale_and_reports_online(session, souk, new_identity):
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "greeter"}])

    listed = await _listed(session, souk)
    assert len(listed) == 1
    assert listed[0]["online"] is True  # just registered, last_seen_at is now()

    # Backdate past the stale-hidden window — should disappear from the
    # roster entirely, not just show online=False.
    await session.execute(
        update(agents).values(last_seen_at=datetime.now(timezone.utc) - timedelta(days=30))
    )
    await session.commit()

    assert await _listed(session, souk) == []


async def test_list_agents_reports_public_key_and_provider_name(session, souk, new_identity):
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "greeter"}], provider_name="Ada's Stall"
    )

    listed = await _listed(session, souk)
    assert listed[0]["public_key"] == identity.public_key
    assert listed[0]["provider_name"] == "Ada's Stall"


async def test_provider_name_defaults_to_none_and_is_sticky_across_registrations(session, souk, new_identity):
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "greeter"}])
    assert (await _listed(session, souk))[0]["provider_name"] is None

    await repo.register_agents(session, identity.public_key, [{"name": "greeter"}], provider_name="Ada's Stall"
    )
    assert (await _listed(session, souk))[0]["provider_name"] == "Ada's Stall"

    # A later registration that doesn't pass provider_name at all must not
    # blank out the name a previous one set.
    await repo.register_agents(session, identity.public_key, [{"name": "greeter"}])
    assert (await _listed(session, souk))[0]["provider_name"] == "Ada's Stall"


async def test_resolve_agents_by_name_zero_one_many(session, new_identity):
    assert await repo.resolve_agents_by_name(session, "nobody") == []

    a = new_identity()
    await repo.register_agents(session, a.public_key, [{"name": "greeter"}])
    assert len(await repo.resolve_agents_by_name(session, "greeter")) == 1

    b = new_identity()
    await repo.register_agents(session, b.public_key, [{"name": "greeter"}])
    assert len(await repo.resolve_agents_by_name(session, "greeter")) == 2


async def test_an_agent_is_addressable_by_whose_it_is_and_what_it_is_called(session, new_identity):
    """The pair is the natural key, so addressing by it can never be
    ambiguous — unlike a name on its own, which two identities may both
    register (see the test above). This is what lets a caller reach one
    particular agent without knowing souk's own id for it."""
    a, b = new_identity(), new_identity()
    mine = await repo.register_agents(session, a.public_key, [{"name": "translator"}])
    theirs = await repo.register_agents(session, b.public_key, [{"name": "translator"}])

    assert (await repo.resolve_agent(session, a.public_key, "translator"))["agent_id"] == mine["translator"]
    assert (await repo.resolve_agent(session, b.public_key, "translator"))["agent_id"] == theirs["translator"]
    # ...while the name alone still answers with both, which is the other
    # question: who offers this, not which one do I mean.
    assert len(await repo.resolve_agents_by_name(session, "translator")) == 2


async def test_resolving_an_agent_a_provider_never_registered_is_a_miss(session, new_identity):
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "translator"}])

    assert await repo.resolve_agent(session, identity.public_key, "summarizer") is None
    assert await repo.resolve_agent(session, new_identity().public_key, "translator") is None


async def test_a_delisted_agent_is_not_addressable(session, new_identity):
    """Same rule as every other lookup: de-listed is not found, and the
    audit trail survives regardless."""
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    await repo.register_agents(session, identity.public_key, [{"name": "summarizer"}])

    assert await repo.resolve_agent(session, identity.public_key, "translator") is None
    assert await repo.resolve_agent(session, identity.public_key, "summarizer") is not None


async def test_a_provider_is_addressable_by_its_fingerprint(session, new_identity):
    """The short form of a 64-character key, so an address can be read and
    typed. Derived, so it is never out of step with the key."""
    identity = new_identity()
    ids = await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    fingerprint = provider_fingerprint(identity.public_key)

    by_key = await repo.resolve_agent(session, identity.public_key, "translator")
    by_fingerprint = await repo.resolve_agent(session, fingerprint, "translator")

    assert by_fingerprint == by_key
    assert by_fingerprint["agent_id"] == ids["translator"]


async def test_an_identity_that_never_named_itself_is_still_addressable(session, new_identity):
    """`providers` records identities, not labels — a short address has to
    resolve for a key that registered and said nothing else about itself."""
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "solo"}])

    assert await repo.resolve_agent(session, provider_fingerprint(identity.public_key), "solo")


async def test_a_second_key_cannot_take_an_existing_fingerprint(session, new_identity):
    """Two identities sharing an address is the one thing an address may not
    do. 64 bits puts a real collision out of reach, so this plants one to
    exercise what the constraint does with it.
    """
    mine, theirs = new_identity(), new_identity()
    await repo.register_agents(session, mine.public_key, [{"name": "translator"}])
    await session.execute(
        update(providers)
        .where(providers.c.public_key == mine.public_key)
        .values(fingerprint=provider_fingerprint(theirs.public_key))
    )
    await session.commit()

    with pytest.raises(repo.ProviderFingerprintTaken):
        await repo.register_agents(session, theirs.public_key, [{"name": "impostor"}])

    # Refused outright: the colliding registration left nothing behind.
    assert await repo.get_agent_ids_for_public_key(session, theirs.public_key) == set()


async def test_junk_resolves_to_nothing_rather_than_raising(session, new_identity):
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "translator"}])

    for junk in ("", "nope", "z" * 16, provider_fingerprint(identity.public_key).upper()):
        assert await repo.resolve_agent(session, junk, "translator") is None
