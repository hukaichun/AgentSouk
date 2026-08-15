"""initial schema

Revision ID: b1e0d9c73a55
Revises:
Create Date: 2026-08-15 18:10:00.000000

souk's baseline schema, built with dialect-neutral SQLAlchemy Core ops so
it renders correctly on both SQLite (souk's zero-config default) and
Postgres — see souk/schema.py, the single source of truth these ops mirror,
for the per-table/column reasoning. This replaces the original Postgres-only
raw-SQL DDL blob (JSONB/TIMESTAMPTZ/BIGSERIAL plus a pgcrypto-backed
`souk_new_id()` function used as column DEFAULTs); entity ids are now minted
in Python (souk.ids.new_id) and passed explicitly on insert, so there is no
DB extension or DB-side id function to create here anymore.

This file is intentionally self-contained (it does not import souk.schema):
it is a frozen snapshot of the schema at this revision. Later schema changes
are new migrations, not edits to this file.

It replaces a four-revision chain — the original baseline plus three changes
made within days of it: a `cancelling` run status, dropping the
`sdk_client_id` column, and giving `providers` a fingerprint. souk has never
been released and no database has ever run those migrations outside a
developer's machine, so the history was worth less than the confusion of a
baseline that creates a column a later revision deletes. *Why* each change
happened is in the commits and in docs/library-architecture.md, which is
where reasons belong; a migration chain only has to produce the schema.

Anyone holding a database created by the old chain should recreate it — its
alembic_version names a revision that no longer exists.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b1e0d9c73a55"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# JSONB on Postgres, plain JSON (TEXT + JSON1) on SQLite. TIMESTAMPTZ on
# Postgres, ISO string on SQLite. BIGINT identity on Postgres, but exactly
# `INTEGER PRIMARY KEY` on SQLite (the only form SQLite auto-increments as
# rowid). Kept in sync with souk/schema.py's _JSON / _TS / _BIGSERIAL.
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
_TS = sa.DateTime(timezone=True)
_BIGSERIAL = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "providers",
        sa.Column("public_key", sa.String(), primary_key=True),
        # Derived from public_key (souk.identity.provider_fingerprint), and
        # UNIQUE: it is an address, and two identities may not share one.
        sa.Column("fingerprint", sa.String(), nullable=False),
        # NULL means this identity never named itself, which is ordinary —
        # the row exists because the identity registered, not because it
        # chose a label.
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("updated_at", _TS, nullable=False),
        sa.UniqueConstraint("fingerprint", name="uq_providers_fingerprint"),
    )

    op.create_table(
        "agents",
        sa.Column("agent_id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("public_key", sa.String(), nullable=False),
        sa.Column("agent_card", _JSON, nullable=False),
        sa.Column("metadata", _JSON, nullable=False),
        sa.Column("joined_at", _TS, nullable=False),
        sa.Column("last_seen_at", _TS, nullable=False),
        sa.Column("delisted_at", _TS, nullable=True),
        sa.UniqueConstraint("public_key", "name", name="uq_agents_public_key_name"),
    )

    op.create_table(
        "threads",
        sa.Column("thread_id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agents.agent_id"), nullable=False),
        sa.Column("parent_thread_id", sa.String(), sa.ForeignKey("threads.thread_id"), nullable=True),
        sa.Column("metadata", _JSON, nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("last_activity_at", _TS, nullable=False),
    )
    op.create_index("idx_threads_parent", "threads", ["parent_thread_id"])

    op.create_table(
        "thread_history",
        sa.Column("id", _BIGSERIAL, primary_key=True, autoincrement=True),
        sa.Column("thread_id", sa.String(), sa.ForeignKey("threads.thread_id"), nullable=False),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("message_id", sa.String(), nullable=True),
        sa.Column("message_json", _JSON, nullable=True),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("protocol", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("input_json", _JSON, nullable=True),
        sa.Column("started_at", _TS, nullable=True),
        sa.Column("completed_at", _TS, nullable=True),
        sa.Column("last_activity_at", _TS, nullable=True),
        sa.Column("metadata", _JSON, nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.CheckConstraint("kind IN ('message', 'run_status')", name="ck_thread_history_kind"),
        sa.CheckConstraint("protocol IN ('ag-ui', 'a2a')", name="ck_thread_history_protocol"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'input-required', 'resumed', 'cancelling', "
            "'completed', 'failed', 'cancelled')",
            name="ck_thread_history_status",
        ),
        sa.UniqueConstraint("thread_id", "message_id", name="uq_thread_history_thread_message"),
    )
    op.create_index("idx_thread_history_thread", "thread_history", ["thread_id", "id"])
    op.create_index(
        "idx_thread_history_run_status_run_id",
        "thread_history",
        ["run_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'run_status'"),
        sqlite_where=sa.text("kind = 'run_status'"),
    )

    op.create_table(
        "run_events",
        sa.Column("id", _BIGSERIAL, primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_json", _JSON, nullable=False),
        sa.Column("created_at", _TS, nullable=False),
    )
    op.create_index("idx_run_events_run", "run_events", ["run_id", "seq"])


def downgrade() -> None:
    op.drop_table("run_events")
    op.drop_table("thread_history")
    op.drop_table("threads")
    op.drop_table("agents")
    op.drop_table("providers")
