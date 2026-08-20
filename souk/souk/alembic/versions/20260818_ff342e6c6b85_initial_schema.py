"""initial schema

Revision ID: ff342e6c6b85
Revises: 
Create Date: 2026-08-18 04:44:51.394323

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = 'ff342e6c6b85'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('providers',
    sa.Column('public_key', sa.String(), nullable=False),
    sa.Column('fingerprint', sa.String(), nullable=False),
    sa.Column('display_name', sa.String(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('public_key'),
    sa.UniqueConstraint('fingerprint')
    )
    op.create_table('agents',
    sa.Column('provider_key', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('agent_card', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('joined_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['provider_key'], ['providers.public_key'], ),
    sa.PrimaryKeyConstraint('provider_key', 'name')
    )
    op.create_table('llm_providers',
    sa.Column('provider_key', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('joined_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['provider_key'], ['providers.public_key'], ),
    sa.PrimaryKeyConstraint('provider_key', 'name')
    )
    op.create_table('threads',
    sa.Column('thread_id', sa.String(), nullable=False),
    sa.Column('provider_key', sa.String(), nullable=False),
    sa.Column('agent_name', sa.String(), nullable=False),
    sa.Column('parent_thread_id', sa.String(), nullable=True),
    sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_activity_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['parent_thread_id'], ['threads.thread_id'], ),
    sa.ForeignKeyConstraint(['provider_key', 'agent_name'], ['agents.provider_key', 'agents.name'], name='fk_threads_agent'),
    sa.PrimaryKeyConstraint('thread_id')
    )
    op.create_index('idx_threads_parent', 'threads', ['parent_thread_id'], unique=False)
    op.create_table('runs',
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('thread_id', sa.String(), nullable=False),
    sa.Column('provider_key', sa.String(), nullable=False),
    sa.Column('agent_name', sa.String(), nullable=False),
    sa.Column('protocol', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('input_json', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_activity_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("protocol IN ('ag-ui', 'a2a')", name='ck_runs_protocol'),
    sa.CheckConstraint("status IN ('queued', 'running', 'input-required', 'cancelling', 'completed', 'failed', 'cancelled')", name='ck_runs_status'),
    sa.ForeignKeyConstraint(['provider_key', 'agent_name'], ['agents.provider_key', 'agents.name'], name='fk_runs_agent'),
    sa.ForeignKeyConstraint(['thread_id'], ['threads.thread_id'], ),
    sa.PrimaryKeyConstraint('run_id')
    )
    op.create_index('idx_runs_agent_status', 'runs', ['provider_key', 'agent_name', 'status'], unique=False)
    op.create_index('idx_runs_thread', 'runs', ['thread_id', 'created_at'], unique=False)
    op.create_table('run_events',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('event_json', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.run_id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('idx_run_events_run', 'run_events', ['run_id', 'seq'], unique=False)
    op.create_table('thread_messages',
    sa.Column('id', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('thread_id', sa.String(), nullable=False),
    sa.Column('run_id', sa.String(), nullable=False),
    sa.Column('message_id', sa.String(), nullable=False),
    sa.Column('message_json', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('metadata', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.run_id'], ),
    sa.ForeignKeyConstraint(['thread_id'], ['threads.thread_id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('thread_id', 'message_id', name='uq_thread_messages_thread_message')
    )
    op.create_index('idx_thread_messages_thread', 'thread_messages', ['thread_id', 'id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('idx_thread_messages_thread', table_name='thread_messages')
    op.drop_table('thread_messages')
    op.drop_index('idx_run_events_run', table_name='run_events')
    op.drop_table('run_events')
    op.drop_index('idx_runs_thread', table_name='runs')
    op.drop_index('idx_runs_agent_status', table_name='runs')
    op.drop_table('runs')
    op.drop_index('idx_threads_parent', table_name='threads')
    op.drop_table('threads')
    op.drop_table('llm_providers')
    op.drop_table('agents')
    op.drop_table('providers')
