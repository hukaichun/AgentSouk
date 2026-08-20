from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update

from funduq import repo
from funduq.identity import provider_fingerprint
from funduq.models import AgentRef
from funduq.schema import agents, providers


async def _listed(session, funduq):
    return await repo.list_agents(
        session, stale_hidden_window_seconds=funduq.settings.stale_hidden_window_seconds
    )


async def test_registering_the_same_agent_twice_is_the_same_agent(session, new_identity):
    identity = new_identity()
    batch = [{"name": "greeter", "description": "hi"}]

    first = await repo.register_agents(session, identity.public_key, batch)
    joined_at = (
        await session.execute(
            select(agents.c.joined_at).where(agents.c.provider_key == identity.public_key)
        )
    ).scalars().one()
    second = await repo.register_agents(session, identity.public_key, batch)

    assert first["greeter"] == second["greeter"]
    rows = (
        await session.execute(
            select(agents.c.joined_at).where(agents.c.provider_key == identity.public_key)
        )
    ).scalars().all()
    assert rows == [joined_at]


async def test_the_same_name_under_two_identities_is_two_agents(session, new_identity):
    a = new_identity()
    b = new_identity()
    batch = [{"name": "greeter"}]

    result_a = await repo.register_agents(session, a.public_key, batch)
    result_b = await repo.register_agents(session, b.public_key, batch)

    assert result_a["greeter"] != result_b["greeter"]
    assert result_a["greeter"].name == result_b["greeter"].name == "greeter"

    for identity, registered in ((a, result_a), (b, result_b)):
        row = await repo.resolve_agent(session, identity.public_key, "greeter")
        assert AgentRef(provider_key=row.provider_key, name=row.name) == registered["greeter"]


async def test_omitting_an_agent_keeps_it_rather_than_removing_it(session, funduq, new_identity):
    identity = new_identity()
    both = [{"name": "greeter"}, {"name": "translator"}]

    registered = await repo.register_agents(session, identity.public_key, both)
    translator = registered["translator"]

    await repo.register_agents(session, identity.public_key, [{"name": "greeter"}])

    assert {a.name for a in await _listed(session, funduq)} == {"greeter", "translator"}
    assert await repo.resolve_agent(session, identity.public_key, "translator") is not None
    assert await repo.get_agent(session, translator) is not None


async def test_list_agents_excludes_an_agent_nothing_has_heard_from_in_weeks(
    session, funduq, new_identity
):
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "greeter"}])

    assert len(await _listed(session, funduq)) == 1

    await session.execute(
        update(agents).values(last_seen_at=datetime.now(timezone.utc) - timedelta(days=30))
    )
    await session.commit()

    assert await _listed(session, funduq) == []


async def test_list_agents_reports_public_key_and_provider_name(session, funduq, new_identity):
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "greeter"}], provider_name="Ada's Stall"
    )

    listed = await _listed(session, funduq)
    assert listed[0].provider_key == identity.public_key
    assert listed[0].provider_name == "Ada's Stall"


async def test_provider_name_defaults_to_none_and_is_sticky_across_registrations(session, funduq, new_identity):
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "greeter"}])
    assert (await _listed(session, funduq))[0].provider_name is None

    await repo.register_agents(session, identity.public_key, [{"name": "greeter"}], provider_name="Ada's Stall"
    )
    assert (await _listed(session, funduq))[0].provider_name == "Ada's Stall"

    await repo.register_agents(session, identity.public_key, [{"name": "greeter"}])
    assert (await _listed(session, funduq))[0].provider_name == "Ada's Stall"


async def test_an_agent_is_addressable_by_whose_it_is_and_what_it_is_called(session, new_identity):
    a, b = new_identity(), new_identity()
    mine = await repo.register_agents(session, a.public_key, [{"name": "translator"}])
    theirs = await repo.register_agents(session, b.public_key, [{"name": "translator"}])

    resolved_a = await repo.resolve_agent(session, a.public_key, "translator")
    resolved_b = await repo.resolve_agent(session, b.public_key, "translator")
    assert AgentRef(provider_key=resolved_a.provider_key, name=resolved_a.name) == mine["translator"]
    assert AgentRef(provider_key=resolved_b.provider_key, name=resolved_b.name) == theirs["translator"]


async def test_resolving_an_agent_a_provider_never_registered_is_a_miss(session, new_identity):
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "translator"}])

    assert await repo.resolve_agent(session, identity.public_key, "summarizer") is None
    assert await repo.resolve_agent(session, new_identity().public_key, "translator") is None


async def test_an_agent_that_went_quiet_is_still_addressable(session, new_identity):
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    await repo.register_agents(session, identity.public_key, [{"name": "summarizer"}])

    assert await repo.resolve_agent(session, identity.public_key, "translator") is not None
    assert await repo.resolve_agent(session, identity.public_key, "summarizer") is not None


async def test_a_provider_is_addressable_by_its_fingerprint(session, new_identity):
    identity = new_identity()
    ids = await repo.register_agents(session, identity.public_key, [{"name": "translator"}])
    fingerprint = provider_fingerprint(identity.public_key)

    by_key = await repo.resolve_agent(session, identity.public_key, "translator")
    by_fingerprint = await repo.resolve_agent(session, fingerprint, "translator")

    assert by_fingerprint == by_key
    assert AgentRef(
        provider_key=by_fingerprint.provider_key, name=by_fingerprint.name
    ) == ids["translator"]


async def test_an_identity_that_never_named_itself_is_still_addressable(session, new_identity):
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "solo"}])

    assert await repo.resolve_agent(session, provider_fingerprint(identity.public_key), "solo")


async def test_a_second_key_cannot_take_an_existing_fingerprint(session, new_identity):
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

    assert await repo.get_agent_names_for_provider(session, theirs.public_key) == set()


async def test_junk_resolves_to_nothing_rather_than_raising(session, new_identity):
    identity = new_identity()
    await repo.register_agents(session, identity.public_key, [{"name": "translator"}])

    for junk in ("", "nope", "z" * 16, provider_fingerprint(identity.public_key).upper()):
        assert await repo.resolve_agent(session, junk, "translator") is None
