"""Add head_key to threads and runs: the responsibility segment's authority.

A thread whose first run carries an actor chain binds the chain's head at
birth (immutable; NULL = unbound, today's open behavior). A run records the
head it arrived under, so a paused run's ask knows whose signature may
resolve it. See docs/mechanisms/responsibility-chains.md.

Revision ID: a11c3b7d42e9
Revises: ff342e6c6b85
Create Date: 2026-08-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a11c3b7d42e9"
down_revision: Union[str, Sequence[str], None] = "ff342e6c6b85"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("threads", sa.Column("head_key", sa.String(), nullable=True))
    op.add_column("runs", sa.Column("head_key", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "head_key")
    op.drop_column("threads", "head_key")
