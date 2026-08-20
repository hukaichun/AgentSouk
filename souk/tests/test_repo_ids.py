from __future__ import annotations

from souk import repo
from souk.models import AgentRef


async def test_registering_mints_no_id_at_all(session, new_identity):
    identity = new_identity()

    registered = await repo.register_agents(session, identity.public_key, [{"name": "a"}])

    assert registered["a"] == AgentRef(provider_key=identity.public_key, name="a")
    assert registered["a"].provider_key == identity.public_key


async def test_create_thread_assigns_a_database_generated_thread_id(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "a"}])
    thread_id = await repo.create_thread(session, registered["a"])
    assert thread_id.startswith("thread_")


async def test_ensure_thread_mints_a_fresh_thread_when_neither_id_is_given(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "a"}])
    thread_id = await repo.ensure_thread(session, registered["a"], None)
    assert thread_id.startswith("thread_")


async def test_ensure_thread_rejects_an_unknown_thread_id_by_default(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "a"}])
    try:
        await repo.ensure_thread(session, registered["a"], "thread_made_up")
        assert False, "expected ThreadNotFound"
    except repo.ThreadNotFound:
        pass


async def test_ensure_thread_creates_under_an_unknown_id_when_told_to(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "a"}])
    thread_id = await repo.ensure_thread(
        session, registered["a"], "thread_made_up", create_if_missing=True
    )
    assert thread_id.startswith("thread_")
    assert thread_id != "thread_made_up"


async def test_create_run_assigns_a_database_generated_run_id(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "a"}])
    thread_id = await repo.create_thread(session, registered["a"])

    first = await repo.create_run(session, thread_id, registered["a"], "ag-ui", {})
    assert first["run_id"].startswith("run_")

    second = await repo.create_run(session, thread_id, registered["a"], "ag-ui", {})
    assert second["run_id"] != first["run_id"]


async def test_append_thread_messages_discards_any_caller_supplied_id(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "a"}])
    thread_id = await repo.create_thread(session, registered["a"])
    run = await repo.create_run(session, thread_id, registered["a"], "ag-ui", {})

    stored = await repo.append_thread_messages(
        session,
        thread_id,
        run["run_id"],
        [{"id": "whatever-the-caller-made-up", "role": "user", "content": "hi"}],
    )
    assert stored[0]["id"] != "whatever-the-caller-made-up"
    assert stored[0]["id"].startswith("msg_")

    persisted = await repo.get_thread_messages(session, thread_id)
    assert persisted[0]["id"] == stored[0]["id"]


async def test_append_thread_messages_never_deduplicates_by_content(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "a"}])
    thread_id = await repo.create_thread(session, registered["a"])
    run = await repo.create_run(session, thread_id, registered["a"], "ag-ui", {})

    same_content = [{"role": "user", "content": "hi"}]
    await repo.append_thread_messages(session, thread_id, run["run_id"], same_content)
    await repo.append_thread_messages(session, thread_id, run["run_id"], same_content)

    persisted = await repo.get_thread_messages(session, thread_id)
    assert len(persisted) == 2
    assert persisted[0]["id"] != persisted[1]["id"]


async def test_create_if_missing_keeps_the_parent_it_was_given(session, new_identity):
    identity = new_identity()
    registered = await repo.register_agents(session, identity.public_key, [{"name": "a"}])
    parent = await repo.create_thread(session, registered["a"])

    child = await repo.ensure_thread(
        session, registered["a"], "never-seen", parent, create_if_missing=True
    )

    stored = await repo.get_thread(session, child)
    assert stored["parent_thread_id"] == parent
