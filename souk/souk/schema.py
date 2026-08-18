from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

metadata = MetaData()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_JSON = JSON().with_variant(JSONB(), "postgresql")

_TS = DateTime(timezone=True)

_BIGSERIAL = BigInteger().with_variant(Integer(), "sqlite")


providers = Table(
    "providers",
    metadata,
    Column("public_key", String, primary_key=True),
    Column("fingerprint", String, nullable=False, unique=True),
    Column("display_name", String, nullable=True),
    Column("updated_at", _TS, nullable=False, default=_utcnow),
)


agents = Table(
    "agents",
    metadata,
    Column("provider_key", String, ForeignKey("providers.public_key"), primary_key=True),
    Column("name", String, primary_key=True),
    Column("agent_card", _JSON, nullable=False),
    Column("metadata", _JSON, nullable=False, default=dict),
    Column("joined_at", _TS, nullable=False, default=_utcnow),
    Column("last_seen_at", _TS, nullable=False, default=_utcnow),
)


llm_providers = Table(
    "llm_providers",
    metadata,
    Column("provider_key", String, ForeignKey("providers.public_key"), primary_key=True),
    Column("name", String, primary_key=True),
    Column("metadata", _JSON, nullable=False, default=dict),
    Column("joined_at", _TS, nullable=False, default=_utcnow),
    Column("last_seen_at", _TS, nullable=False, default=_utcnow),
)


threads = Table(
    "threads",
    metadata,
    Column("thread_id", String, primary_key=True),
    Column("provider_key", String, nullable=False),
    Column("agent_name", String, nullable=False),
    Column("parent_thread_id", String, ForeignKey("threads.thread_id"), nullable=True),
    Column("metadata", _JSON, nullable=False, default=dict),
    Column("created_at", _TS, nullable=False, default=_utcnow),
    Column("last_activity_at", _TS, nullable=False, default=_utcnow),
    ForeignKeyConstraint(
        ["provider_key", "agent_name"], ["agents.provider_key", "agents.name"],
        name="fk_threads_agent",
    ),
    Index("idx_threads_parent", "parent_thread_id"),
)


runs = Table(
    "runs",
    metadata,
    Column("run_id", String, primary_key=True),
    Column("thread_id", String, ForeignKey("threads.thread_id"), nullable=False),
    Column("provider_key", String, nullable=False),
    Column("agent_name", String, nullable=False),
    Column("protocol", String, nullable=False),
    Column("status", String, nullable=False),
    Column("input_json", _JSON, nullable=False),
    Column("started_at", _TS, nullable=True),
    Column("completed_at", _TS, nullable=True),
    Column("last_activity_at", _TS, nullable=True),
    Column("metadata", _JSON, nullable=False, default=dict),
    Column("created_at", _TS, nullable=False, default=_utcnow),
    CheckConstraint("protocol IN ('ag-ui', 'a2a')", name="ck_runs_protocol"),
    CheckConstraint(
        "status IN ('queued', 'running', 'input-required', 'cancelling', 'completed', 'failed', 'cancelled')",
        name="ck_runs_status",
    ),
    ForeignKeyConstraint(
        ["provider_key", "agent_name"], ["agents.provider_key", "agents.name"],
        name="fk_runs_agent",
    ),
    Index("idx_runs_thread", "thread_id", "created_at"),
    Index("idx_runs_agent_status", "provider_key", "agent_name", "status"),
)


thread_messages = Table(
    "thread_messages",
    metadata,
    Column("id", _BIGSERIAL, primary_key=True, autoincrement=True),
    Column("thread_id", String, ForeignKey("threads.thread_id"), nullable=False),
    Column("run_id", String, ForeignKey("runs.run_id"), nullable=False),
    Column("message_id", String, nullable=False),
    Column("message_json", _JSON, nullable=False),
    Column("metadata", _JSON, nullable=False, default=dict),
    Column("created_at", _TS, nullable=False, default=_utcnow),
    UniqueConstraint("thread_id", "message_id", name="uq_thread_messages_thread_message"),
    Index("idx_thread_messages_thread", "thread_id", "id"),
)


run_events = Table(
    "run_events",
    metadata,
    Column("id", _BIGSERIAL, primary_key=True, autoincrement=True),
    Column("run_id", String, ForeignKey("runs.run_id"), nullable=False),
    Column("seq", Integer, nullable=False),
    Column("event_json", _JSON, nullable=False),
    Column("created_at", _TS, nullable=False, default=_utcnow),
    Index("idx_run_events_run", "run_id", "seq"),
)
