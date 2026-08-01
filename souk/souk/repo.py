"""Postgres access helpers, kept as plain SQL via SQLAlchemy Core — two
handfuls of tables and simple queries don't need a full ORM or Alembic for
v1 (souk/db.py bootstraps the schema with idempotent CREATE TABLE IF NOT
EXISTS on startup instead).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from souk.config import settings
from souk.ids import new_id


async def register_agents(
    session: AsyncSession, sdk_client_id: str, agents: list[dict[str, Any]]
) -> None:
    for agent in agents:
        card = {
            "name": agent["name"],
            "description": agent.get("description", ""),
            **agent.get("agent_card_extra", {}),
        }
        await session.execute(
            text(
                """
                INSERT INTO agents (name, sdk_client_id, agent_card, metadata, joined_at, last_seen_at)
                VALUES (:name, :sdk_client_id, :agent_card, :metadata, now(), now())
                ON CONFLICT (name) DO UPDATE SET
                    sdk_client_id = EXCLUDED.sdk_client_id,
                    agent_card = EXCLUDED.agent_card,
                    metadata = EXCLUDED.metadata,
                    last_seen_at = now()
                """
            ),
            {
                "name": agent["name"],
                "sdk_client_id": sdk_client_id,
                "agent_card": json.dumps(card),
                "metadata": json.dumps(agent.get("metadata", {})),
            },
        )
    await session.commit()


async def touch_agent(session: AsyncSession, name: str) -> None:
    await session.execute(
        text("UPDATE agents SET last_seen_at = now() WHERE name = :name"),
        {"name": name},
    )
    await session.commit()


async def get_agent(session: AsyncSession, name: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text(
                "SELECT name, agent_card, metadata, joined_at, last_seen_at "
                "FROM agents WHERE name = :name"
            ),
            {"name": name},
        )
    ).mappings().first()
    return dict(row) if row else None


async def list_agents(session: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text("SELECT name, joined_at, last_seen_at FROM agents ORDER BY name")
        )
    ).mappings().all()
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=settings.online_window_seconds)
    return [
        {**dict(row), "online": row["last_seen_at"].replace(tzinfo=timezone.utc) >= cutoff}
        for row in rows
    ]


async def get_thread(session: AsyncSession, thread_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT * FROM threads WHERE thread_id = :thread_id"), {"thread_id": thread_id}
        )
    ).mappings().first()
    return dict(row) if row else None


async def ensure_thread(
    session: AsyncSession,
    agent_name: str,
    thread_id: str | None,
    parent_thread_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Returns the thread_id to use — souk always assigns the id itself
    (see souk.ids); callers never mint their own. Three cases:

    1. `thread_id` given and it already exists: reuse it (must belong to
       `agent_name`). This is for genuine external callers that supply
       their own opaque identifier (e.g. a real A2A client's sessionId) —
       souk just tracks it, it doesn't generate it, so it's exempt from
       the "db-generated" rule the same way A2A's caller-supplied task id
       already is.
    2. `thread_id` is None but `parent_thread_id` is given (e.g. a
       sub-agent call spawned from within another thread's run): reuse
       the existing child thread for this (parent_thread_id, agent_name)
       pair if one exists, so repeated delegation calls keep talking to
       the same sub-thread — otherwise assign a fresh souk-generated id.
    3. Neither given: always assign a fresh souk-generated id.
    """
    if thread_id is not None:
        existing = await get_thread(session, thread_id)
        if existing is not None:
            if existing["agent_name"] != agent_name:
                raise ValueError(
                    f"thread '{thread_id}' belongs to agent '{existing['agent_name']}', not '{agent_name}'"
                )
            await session.execute(
                text("UPDATE threads SET last_activity_at = now() WHERE thread_id = :thread_id"),
                {"thread_id": thread_id},
            )
            return thread_id
    elif parent_thread_id is not None:
        row = (
            await session.execute(
                text(
                    """
                    SELECT thread_id FROM threads
                    WHERE parent_thread_id = :parent_thread_id AND agent_name = :agent_name
                    ORDER BY created_at LIMIT 1
                    """
                ),
                {"parent_thread_id": parent_thread_id, "agent_name": agent_name},
            )
        ).mappings().first()
        if row is not None:
            await session.execute(
                text("UPDATE threads SET last_activity_at = now() WHERE thread_id = :thread_id"),
                {"thread_id": row["thread_id"]},
            )
            return row["thread_id"]
        thread_id = new_id("thread")
    else:
        thread_id = new_id("thread")

    await session.execute(
        text(
            """
            INSERT INTO threads (thread_id, agent_name, parent_thread_id, metadata, created_at, last_activity_at)
            VALUES (:thread_id, :agent_name, :parent_thread_id, :metadata, now(), now())
            """
        ),
        {
            "thread_id": thread_id,
            "agent_name": agent_name,
            "parent_thread_id": parent_thread_id,
            "metadata": json.dumps(metadata or {}),
        },
    )
    return thread_id


async def append_thread_messages(
    session: AsyncSession, thread_id: str, run_id: str, messages: list[dict[str, Any]]
) -> None:
    for index, message in enumerate(messages):
        message_id = str(message.get("id") or f"{run_id}-{index}")
        await session.execute(
            text(
                """
                INSERT INTO thread_history (thread_id, run_id, kind, message_id, message_json, metadata)
                VALUES (:thread_id, :run_id, 'message', :message_id, :message_json, :metadata)
                ON CONFLICT (thread_id, message_id) DO NOTHING
                """
            ),
            {
                "thread_id": thread_id,
                "run_id": run_id,
                "message_id": message_id,
                "message_json": json.dumps(message),
                "metadata": json.dumps(message.get("metadata", {})),
            },
        )


async def get_thread_messages(session: AsyncSession, thread_id: str) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text(
                """
                SELECT message_json FROM thread_history
                WHERE thread_id = :thread_id AND kind = 'message'
                ORDER BY id
                """
            ),
            {"thread_id": thread_id},
        )
    ).mappings().all()
    return [row["message_json"] for row in rows]


async def create_run(
    session: AsyncSession,
    thread_id: str,
    agent_name: str,
    protocol: str,
    input_json: dict[str, Any],
    assign_task_id: bool = False,
    metadata: dict[str, Any] | None = None,
) -> dict[str, str]:
    run_id = new_id("run")
    task_id = new_id("task") if assign_task_id else None
    await session.execute(
        text(
            """
            INSERT INTO thread_history
                (thread_id, run_id, kind, agent_name, protocol, status, input_json, task_id, metadata)
            VALUES
                (:thread_id, :run_id, 'run_status', :agent_name, :protocol, 'queued', :input_json, :task_id, :metadata)
            """
        ),
        {
            "thread_id": thread_id,
            "run_id": run_id,
            "agent_name": agent_name,
            "protocol": protocol,
            "input_json": json.dumps(input_json),
            "task_id": task_id,
            "metadata": json.dumps(metadata or {}),
        },
    )
    await session.commit()
    return {"run_id": run_id, "task_id": task_id}


async def set_task_id(session: AsyncSession, run_id: str, task_id: str) -> None:
    await session.execute(
        text("UPDATE thread_history SET task_id = :task_id WHERE run_id = :run_id AND kind = 'run_status'"),
        {"task_id": task_id, "run_id": run_id},
    )


async def mark_run_status(session: AsyncSession, run_id: str, status: str) -> None:
    timestamp_col = {
        "running": "started_at",
        "completed": "completed_at",
        "failed": "completed_at",
        "cancelled": "completed_at",
    }.get(status)
    extra_set = f", {timestamp_col} = now()" if timestamp_col else ""
    await session.execute(
        text(
            f"UPDATE thread_history SET status = :status{extra_set} "
            "WHERE run_id = :run_id AND kind = 'run_status'"
        ),
        {"status": status, "run_id": str(run_id)},
    )
    await session.commit()


async def fail_orphaned_runs(session: AsyncSession) -> list[str]:
    """Called once on souk startup. souk's live dispatch state (souk.broker)
    is pure in-memory — a restart loses it entirely, so any run still
    'queued' or 'running' in the DB at that point will never be picked up
    or completed again (PollForWork only ever consults the broker, not the
    DB). Mark them 'failed' so the DB stops claiming they're still live.

    Deliberately narrow: the WHERE clause only ever touches rows still in
    a non-terminal state. Runs already 'completed'/'failed'/'cancelled'
    are untouched — every run can fail, but a run that already finished
    keeps the state it finished in, always.
    """
    rows = (
        await session.execute(
            text(
                """
                UPDATE thread_history
                SET status = 'failed',
                    completed_at = now(),
                    metadata = metadata || '{"failureReason": "orphaned_by_souk_restart"}'::jsonb
                WHERE kind = 'run_status' AND status IN ('queued', 'running')
                RETURNING run_id
                """
            )
        )
    ).scalars().all()
    await session.commit()
    return list(rows)


async def get_run(session: AsyncSession, run_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT * FROM thread_history WHERE run_id = :run_id AND kind = 'run_status'"),
            {"run_id": run_id},
        )
    ).mappings().first()
    return dict(row) if row else None


async def get_run_by_task_id(session: AsyncSession, task_id: str) -> dict[str, Any] | None:
    row = (
        await session.execute(
            text("SELECT * FROM thread_history WHERE task_id = :task_id AND kind = 'run_status'"),
            {"task_id": task_id},
        )
    ).mappings().first()
    return dict(row) if row else None


async def append_run_event(session: AsyncSession, run_id: str, seq: int, event_json: dict[str, Any]) -> None:
    await session.execute(
        text(
            "INSERT INTO run_events (run_id, seq, event_json, created_at) VALUES (:run_id, :seq, :event_json, now())"
        ),
        {"run_id": run_id, "seq": seq, "event_json": json.dumps(event_json)},
    )
    await session.commit()


async def get_run_events(session: AsyncSession, run_id: str) -> list[dict[str, Any]]:
    rows = (
        await session.execute(
            text("SELECT event_json FROM run_events WHERE run_id = :run_id ORDER BY seq"),
            {"run_id": run_id},
        )
    ).mappings().all()
    return [row["event_json"] for row in rows]
