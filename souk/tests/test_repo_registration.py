"""Covers the agent_id/soft-delist mechanics in souk.repo.register_agents —
the core of this session's identity rework: name is a non-unique display
label, agent_id (scoped to (public_key, name)) is the real ownership key,
and a registration batch is the declarative full statement of what an
identity currently offers (anything previously owned but omitted gets
soft-delisted).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from souk import repo
from souk.schema import agents


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

    first = await repo.register_agents(session, "sdk_1", identity.public_key, agents)
    second = await repo.register_agents(session, "sdk_1", identity.public_key, agents)

    assert first["greeter"] == second["greeter"]
    assert first["greeter"].startswith("agent_")


async def test_different_identity_same_name_gets_distinct_agent_id(session, new_identity):
    a = new_identity()
    b = new_identity()
    agents = [{"name": "greeter"}]

    result_a = await repo.register_agents(session, "sdk_a", a.public_key, agents)
    result_b = await repo.register_agents(session, "sdk_b", b.public_key, agents)

    assert result_a["greeter"] != result_b["greeter"]

    candidates = await repo.resolve_agents_by_name(session, "greeter")
    assert {c["agent_id"] for c in candidates} == {result_a["greeter"], result_b["greeter"]}


async def test_omitting_an_agent_soft_delists_it_and_reappearing_undoes_it(session, souk, new_identity):
    identity = new_identity()
    both = [{"name": "greeter"}, {"name": "translator"}]

    ids = await repo.register_agents(session, "sdk_1", identity.public_key, both)
    translator_id = ids["translator"]

    # Re-register with translator omitted — the batch is the declarative
    # full statement of what this identity offers now.
    await repo.register_agents(session, "sdk_1", identity.public_key, [{"name": "greeter"}])

    row = (
        await session.execute(
            select(agents.c.delisted_at).where(agents.c.agent_id == translator_id)
        )
    ).mappings().first()
    assert row["delisted_at"] is not None

    names_after_delist = [a["name"] for a in await _listed(session, souk)]
    assert names_after_delist == ["greeter"]

    # Reappearing in a later batch clears delisted_at again (self-heal).
    await repo.register_agents(session, "sdk_1", identity.public_key, both)
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
    await repo.register_agents(session, "sdk_1", identity.public_key, [{"name": "greeter"}])

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
    await repo.register_agents(
        session, "sdk_1", identity.public_key, [{"name": "greeter"}], provider_name="Ada's Stall"
    )

    listed = await _listed(session, souk)
    assert listed[0]["public_key"] == identity.public_key
    assert listed[0]["provider_name"] == "Ada's Stall"


async def test_provider_name_defaults_to_none_and_is_sticky_across_registrations(session, souk, new_identity):
    identity = new_identity()
    await repo.register_agents(session, "sdk_1", identity.public_key, [{"name": "greeter"}])
    assert (await _listed(session, souk))[0]["provider_name"] is None

    await repo.register_agents(
        session, "sdk_1", identity.public_key, [{"name": "greeter"}], provider_name="Ada's Stall"
    )
    assert (await _listed(session, souk))[0]["provider_name"] == "Ada's Stall"

    # A later registration that doesn't pass provider_name at all must not
    # blank out the name a previous one set.
    await repo.register_agents(session, "sdk_1", identity.public_key, [{"name": "greeter"}])
    assert (await _listed(session, souk))[0]["provider_name"] == "Ada's Stall"


async def test_resolve_agents_by_name_zero_one_many(session, new_identity):
    assert await repo.resolve_agents_by_name(session, "nobody") == []

    a = new_identity()
    await repo.register_agents(session, "sdk_1", a.public_key, [{"name": "greeter"}])
    assert len(await repo.resolve_agents_by_name(session, "greeter")) == 1

    b = new_identity()
    await repo.register_agents(session, "sdk_2", b.public_key, [{"name": "greeter"}])
    assert len(await repo.resolve_agents_by_name(session, "greeter")) == 2
