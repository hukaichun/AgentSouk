"""llm_providers

The KYOK redesign: the party answering completions is a first-class LLM
provider with its own registered identity (see souk/schema.py's
llm_providers and docs/keep-your-own-key.md), not an anonymous bridge
session. This table is its durable roster, shaped like `agents`: an
offering is the (provider_key, name) pair, identity rows live in
`providers`.

Revision ID: a3d1c47be902
Revises: fdf80e39f55e
Create Date: 2026-08-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'a3d1c47be902'
down_revision: Union[str, Sequence[str], None] = 'fdf80e39f55e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Kept in sync with souk/schema.py's _JSON.
_JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table('llm_providers',
    sa.Column('provider_key', sa.String(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('metadata', _JSON, nullable=False),
    sa.Column('joined_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['provider_key'], ['providers.public_key'], ),
    sa.PrimaryKeyConstraint('provider_key', 'name')
    )


def downgrade() -> None:
    op.drop_table('llm_providers')
