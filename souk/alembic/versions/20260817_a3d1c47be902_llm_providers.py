from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = 'a3d1c47be902'
down_revision: Union[str, Sequence[str], None] = 'fdf80e39f55e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

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
