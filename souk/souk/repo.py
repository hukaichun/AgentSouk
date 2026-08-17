from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, func, insert, inspect, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from souk.identity import provider_fingerprint
from souk.ids import new_id
from souk.models import AgentRecord, AgentRef, AgentSummary, LlmRef, RunRecord
from souk.schema import (
    agents,
    llm_providers,
    providers,
    run_events,
    runs,
    thread_messages,
    threads,
)


ACTIVE_RUN_STATUSES = ["queued", "running", "cancelling", "input-required"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _upsert(session: AsyncSession, table):
    is_postgres = session.bind.dialect.name == "postgresql"
    return (pg_insert if is_postgres else sqlite_insert)(table)


class ProviderFingerprintTaken(Exception):
    pass


class RunRowMissing(Exception):
    pass


class ThreadNotFound(Exception):
    pass


class ThreadOwnershipMismatch(Exception):
    pass


async def get_schema_revision(session: AsyncSession) -> str | None:
    connection = await session.connection()
    if not await connection.run_sync(lambda c: inspect(c).has_table("alembic_version")):
        return None
    return (
        await session.execute(select(text("version_num")).select_from(text("alembic_version")))
    ).scalars().first()


async def ensure_provider(session: AsyncSession, public_key: str) -> None:
    now = _utcnow()
    stmt = _upsert(session, providers).values(
        public_key=public_key,
        fingerprint=provider_fingerprint(public_key),
        updated_at=now,
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=[providers.c.public_key])
    try:
        await session.execute(stmt)
    except IntegrityError as e:
        await session.rollback()
        raise ProviderFingerprintTaken(
            f"another provider already holds fingerprint {provider_fingerprint(public_key)}"
        ) from e


async def set_provider_name(session: AsyncSession, public_key: str, display_name: str) -> None:
    await session.execute(
        update(providers)
        .where(providers.c.public_key == public_key)
        .values(display_name=display_name, updated_at=_utcnow())
    )


async def register_agents(
    session: AsyncSession,
    public_key: str,
    agents_batch: list[dict[str, Any]],
    provider_name: str | None = None,
) -> dict[str, AgentRef]:
    await ensure_provider(session, public_key)
    if provider_name is not None:
        await set_provider_name(session, public_key, provider_name)

    now = _utcnow()
    registered: dict[str, AgentRef] = {}
    for agent in agents_batch:
        name = agent["name"]
        card = {
            "name": name,
            "description": agent.get("description", ""),
            **agent.get("agent_card_extra", {}),
        }
        stmt = _upsert(session, agents).values(
            name=name,
            provider_key=public_key,
            agent_card=card,
            metadata=agent.get("metadata", {}),
            joined_at=now,
            last_seen_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[agents.c.provider_key, agents.c.name],
            set_={
                "agent_card": stmt.excluded.agent_card,
                "metadata": stmt.excluded.metadata,
                "last_seen_at": now,
            },
        )
        await session.execute(stmt)
        registered[name] = AgentRef(provider_key=public_key, name=name)

    await session.commit()
    return registered


async def get_agent_names_for_provider(session: AsyncSession, provider_key: str) -> set[str]:
    rows = (
        await session.execute(
            select(agents.c.name).where(agents.c.provider_key == provider_key)
        )
    ).scalars().all()
    return set(rows)


async def register_llm_providers(
    session: AsyncSession,
    public_key: str,
    names: list[str],
    metadata: dict[str, Any] | None = None,
) -> dict[str, LlmRef]:
    await ensure_provider(session, public_key)
    now = _utcnow()
    registered: dict[str, LlmRef] = {}
    for name in names:
        stmt = _upsert(session, llm_providers).values(
            provider_key=public_key,
            name=name,
            metadata=metadata or {},
            joined_at=now,
            last_seen_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[llm_providers.c.provider_key, llm_providers.c.name],
            set_={"metadata": stmt.excluded.metadata, "last_seen_at": now},
        )
        await session.execute(stmt)
        registered[name] = LlmRef(provider_key=public_key, name=name)
    await session.commit()
    return registered


async def get_llm_provider(session: AsyncSession, ref: LlmRef) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(llm_providers).where(
                llm_providers.c.provider_key == ref.provider_key,
                llm_providers.c.name == ref.name,
            )
        )
    ).mappings().first()
    return dict(row) if row else None


async def get_llm_names_for_key(session: AsyncSession, public_key: str) -> set[str]:
    rows = (
        await session.execute(
            select(llm_providers.c.name).where(llm_providers.c.provider_key == public_key)
        )
    ).scalars().all()
    return set(rows)


async def touch_llm_providers(
    session: AsyncSession, provider_key: str, names: list[str]
) -> None:
    if not names:
        return
    await session.execute(
        update(llm_providers)
        .where(
            llm_providers.c.provider_key == provider_key,
            llm_providers.c.name.in_(names),
        )
        .values(last_seen_at=_utcnow())
    )
    await session.commit()


async def touch_agents(session: AsyncSession, provider_key: str, names: list[str]) -> None:
    if not names:
        return
    await session.execute(
        update(agents)
        .where(agents.c.provider_key == provider_key, agents.c.name.in_(names))
        .values(last_seen_at=_utcnow())
    )
    await session.commit()


async def get_agent(session: AsyncSession, agent: AgentRef) -> AgentRecord | None:
    row = (
        await session.execute(
            select(
                agents.c.provider_key,
                agents.c.name,
                agents.c.agent_card,
                agents.c.metadata,
                agents.c.joined_at,
                agents.c.last_seen_at,
            ).where(
                agents.c.provider_key == agent.provider_key,
                agents.c.name == agent.name,
            )
        )
    ).mappings().first()
    return AgentRecord(**row) if row else None


async def resolve_agent(session: AsyncSession, provider: str, name: str) -> AgentRecord | None:
    row = (
        await session.execute(
            select(
                agents.c.provider_key,
                agents.c.name,
                agents.c.agent_card,
                agents.c.metadata,
                agents.c.joined_at,
                agents.c.last_seen_at,
            )
            .select_from(agents.outerjoin(providers, providers.c.public_key == agents.c.provider_key))
            .where(
                or_(agents.c.provider_key == provider, providers.c.fingerprint == provider),
                agents.c.name == name,
            )
        )
    ).mappings().first()
    return AgentRecord(**row) if row else None


async def list_agents(
    session: AsyncSession,
    *,
    stale_hidden_window_seconds: int,
) -> list[AgentSummary]:
    stale_cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_hidden_window_seconds)
    rows = (
        await session.execute(
            select(
                agents.c.provider_key,
                agents.c.name,
                agents.c.agent_card,
                agents.c.joined_at,
                agents.c.last_seen_at,
                providers.c.display_name.label("provider_name"),
            )
            .select_from(
                agents.outerjoin(providers, providers.c.public_key == agents.c.provider_key)
            )
            .where(agents.c.last_seen_at >= stale_cutoff)
            .order_by(agents.c.name)
        )
    ).mappings().all()
    return [
        AgentSummary(
            provider_key=row["provider_key"],
            name=row["name"],
            description=row["agent_card"].get("description", ""),
            skills=row["agent_card"].get("skills", []),
            joined_at=row["joined_at"],
            last_seen_at=row["last_seen_at"],
            provider_name=row["provider_name"],
        )
        for row in rows
    ]


async def count_threads_for_agent(session: AsyncSession, agent: AgentRef) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(threads)
            .where(
                threads.c.provider_key == agent.provider_key,
                threads.c.agent_name == agent.name,
            )
        )
    ).scalar_one()


async def count_runs_for_agent(session: AsyncSession, agent: AgentRef, statuses: list[str] | None = None) -> int:
    where = [runs.c.provider_key == agent.provider_key, runs.c.agent_name == agent.name]
    if statuses is not None:
        where.append(runs.c.status.in_(statuses))
    return (
        await session.execute(select(func.count()).select_from(runs).where(*where))
    ).scalar_one()


async def delete_agent(session: AsyncSession, agent: AgentRef) -> bool:
    result = await session.execute(
        delete(agents).where(
            agents.c.provider_key == agent.provider_key, agents.c.name == agent.name
        )
    )
    await session.commit()
    return result.rowcount > 0


async def get_agent_name_for_public_key(session: AsyncSession, public_key: str) -> str | None:
    return (
        await session.execute(
            select(agents.c.name)
            .where(agents.c.provider_key == public_key)
            .order_by(agents.c.joined_at)
            .limit(1)
        )
    ).scalars().first()


async def get_thread(session: AsyncSession, thread_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(select(threads).where(threads.c.thread_id == thread_id))
    ).mappings().first()
    return dict(row) if row else None


async def create_thread(
    session: AsyncSession,
    agent: AgentRef,
    parent_thread_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    thread_id = new_id("thread")
    now = _utcnow()
    await session.execute(
        insert(threads).values(
            thread_id=thread_id,
            provider_key=agent.provider_key,
            agent_name=agent.name,
            parent_thread_id=parent_thread_id,
            metadata=metadata or {},
            created_at=now,
            last_activity_at=now,
        )
    )
    return thread_id


async def ensure_thread(
    session: AsyncSession,
    agent: AgentRef,
    thread_id: str | None,
    parent_thread_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    create_if_missing: bool = False,
) -> str:
    if thread_id is not None:
        existing = await get_thread(session, thread_id)
        if existing is None:
            if create_if_missing:
                return await create_thread(session, agent, metadata=metadata)
            raise ThreadNotFound(thread_id)
        owner = AgentRef(
            provider_key=existing["provider_key"], name=existing["agent_name"]
        )
        if owner != agent:
            raise ThreadOwnershipMismatch(
                f"thread '{thread_id}' belongs to agent '{owner}', not '{agent}'"
            )
        await session.execute(
            update(threads).where(threads.c.thread_id == thread_id).values(last_activity_at=_utcnow())
        )
        return thread_id

    return await create_thread(session, agent, parent_thread_id, metadata)


async def get_thread_children(session: AsyncSession, thread_id: str) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(
                threads.c.thread_id,
                threads.c.provider_key,
                threads.c.agent_name,
                threads.c.created_at,
            )
            .where(threads.c.parent_thread_id == thread_id)
            .order_by(threads.c.created_at)
        )
    ).mappings().all()
    return [dict(row) for row in rows]


async def append_thread_messages(
    session: AsyncSession, thread_id: str, run_id: str, messages: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    stored: list[dict[str, Any]] = []
    for message in messages:
        message_id = new_id("msg")
        final_message = {**message, "id": message_id}
        await session.execute(
            insert(thread_messages).values(
                thread_id=thread_id,
                run_id=run_id,
                message_id=message_id,
                message_json=final_message,
                metadata=message.get("metadata", {}),
            )
        )
        stored.append(final_message)
    return stored


async def get_thread_messages(session: AsyncSession, thread_id: str) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(thread_messages.c.message_json)
            .where(thread_messages.c.thread_id == thread_id)
            .order_by(thread_messages.c.id)
        )
    ).all()
    return [row.message_json for row in rows]


async def create_run(
    session: AsyncSession,
    thread_id: str,
    agent: AgentRef,
    protocol: str,
    input_json: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    run_id = new_id("run")
    await session.execute(
        insert(runs).values(
            run_id=run_id,
            thread_id=thread_id,
            provider_key=agent.provider_key,
            agent_name=agent.name,
            protocol=protocol,
            status="queued",
            input_json=input_json,
            metadata=metadata or {},
            last_activity_at=_utcnow(),
        )
    )
    await session.commit()
    return {"run_id": run_id}


async def _merge_run_metadata(
    session: AsyncSession, run_id: str, metadata: dict[str, Any]
) -> dict[str, Any]:
    existing = (
        await session.execute(
            select(runs.c.metadata).where(runs.c.run_id == run_id)
        )
    ).scalars().first()
    return {**(existing or {}), **metadata}


async def reopen_run(
    session: AsyncSession, run_id: str, input_json: dict[str, Any], metadata: dict[str, Any] | None = None
) -> None:
    values: dict[str, Any] = {
        "status": "queued",
        "input_json": input_json,
        "last_activity_at": _utcnow(),
    }
    if metadata:
        values["metadata"] = await _merge_run_metadata(session, run_id, metadata)
    await session.execute(
        update(runs).where(runs.c.run_id == run_id).values(**values)
    )
    await session.commit()


async def mark_run_status(
    session: AsyncSession, run_id: str, status: str, metadata: dict[str, Any] | None = None
) -> None:
    timestamp_col = {
        "running": "started_at",
        "completed": "completed_at",
        "failed": "completed_at",
        "cancelled": "completed_at",
    }.get(status)
    now = _utcnow()
    values: dict[str, Any] = {"status": status, "last_activity_at": now}
    if timestamp_col:
        values[timestamp_col] = now
    if metadata:
        values["metadata"] = await _merge_run_metadata(session, run_id, metadata)
    result = await session.execute(
        update(runs).where(runs.c.run_id == str(run_id)).values(**values)
    )
    await session.commit()
    if result.rowcount == 0:
        raise RunRowMissing(
            f"run {run_id}: no such run in the database — souk is dispatching a run "
            "this database does not have"
        )


async def get_active_run_for_thread(session: AsyncSession, thread_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            select(runs)
            .where(
                runs.c.thread_id == thread_id,
                runs.c.status.in_(ACTIVE_RUN_STATUSES),
            )
            .order_by(runs.c.created_at.desc())
            .limit(1)
        )
    ).mappings().first()
    return dict(row) if row else None


async def get_thread_snapshot(session: AsyncSession, thread_id: str) -> dict[str, Any] | None:
    thread = await get_thread(session, thread_id)
    if thread is None:
        return None
    messages = await get_thread_messages(session, thread_id)
    active_run = await get_active_run_for_thread(session, thread_id)
    return {"thread_id": thread_id, "messages": messages, "active_run": active_run}


async def touch_run_activity(session: AsyncSession, run_id: str) -> None:
    await session.execute(
        update(runs).where(runs.c.run_id == run_id).values(last_activity_at=_utcnow())
    )


async def _fail_runs(
    session: AsyncSession, where_clause, failure_reason: str
) -> list[str]:
    rows = (
        await session.execute(
            select(runs.c.run_id, runs.c.metadata).where(where_clause)
        )
    ).all()
    now = _utcnow()
    run_ids: list[str] = []
    for row in rows:
        await session.execute(
            update(runs)
            .where(runs.c.run_id == row.run_id)
            .values(
                status="failed",
                completed_at=now,
                metadata={**(row.metadata or {}), "failureReason": failure_reason},
            )
        )
        run_ids.append(row.run_id)
    await session.commit()
    return run_ids


async def fail_orphaned_runs(session: AsyncSession) -> list[str]:
    return await _fail_runs(
        session,
        runs.c.status.in_(["queued", "running", "cancelling"]),
        "orphaned_by_souk_restart",
    )


async def fail_stalled_runs(session: AsyncSession, stall_timeout_seconds: int) -> list[str]:
    cutoff = _utcnow() - timedelta(seconds=stall_timeout_seconds)
    return await _fail_runs(
        session,
        runs.c.status.in_(["running", "cancelling"]) & (runs.c.last_activity_at < cutoff),
        "stalled_no_activity",
    )


async def fail_stale_paused_runs(session: AsyncSession, timeout_seconds: int) -> list[str]:
    cutoff = _utcnow() - timedelta(seconds=timeout_seconds)
    return await _fail_runs(
        session,
        (runs.c.status == "input-required") & (runs.c.last_activity_at < cutoff),
        "paused_no_resume",
    )


async def get_run(session: AsyncSession, run_id: str) -> RunRecord | None:
    row = (
        await session.execute(select(runs).where(runs.c.run_id == run_id))
    ).mappings().first()
    return RunRecord(**row) if row else None


async def append_run_event(session: AsyncSession, run_id: str, seq: int, event_json: dict[str, Any]) -> None:
    await session.execute(
        insert(run_events).values(run_id=run_id, seq=seq, event_json=event_json, created_at=_utcnow())
    )
    await session.commit()


async def get_run_events(session: AsyncSession, run_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            select(run_events.c.event_json)
            .where(run_events.c.run_id == run_id, run_events.c.seq > since_seq)
            .order_by(run_events.c.seq)
        )
    ).all()
    return [row.event_json for row in rows]


async def get_last_event_seq(session: AsyncSession, run_id: str) -> int:
    return (
        await session.execute(
            select(func.coalesce(func.max(run_events.c.seq), 0)).where(run_events.c.run_id == run_id)
        )
    ).scalar()
