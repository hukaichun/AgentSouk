"""drop agents.sdk_client_id

Revision ID: a3f1c07d2b94
Revises: 62eece4bcbb1
Create Date: 2026-08-15 12:40:00.000000

A provider's identity is its Ed25519 keypair, and `agents.public_key` already
records it: agent_id is assigned per (public_key, name), de-listing sweeps by
public_key, and the providers table is keyed by it. `sdk_client_id` held
whatever string the registering client happened to call itself, and claiming
filtered on *that*. Two things were measured before removing it:

- two unrelated keypairs registering under the same string were both
  accepted, and the second one's session token claimed the first one's run,
  received its input, and could report events into it;
- two processes of one real identity (the SDK mints a random string per
  process) fought over the column, because a registration overwrites it —
  after the second registered, the first could no longer claim its own
  agent's work, with a valid token and a live connection.

So it was not an identity and not a usable per-process label. Claiming,
reporting and the session token all key off public_key now.

Dropping a column differs by dialect: Postgres does it in place, SQLite
cannot and needs the table rebuilt, which alembic's batch mode does by
copying into a new one. Both paths are spelled out rather than hidden, the
same as the CHECK-constraint migration before this.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3f1c07d2b94"
down_revision: Union[str, Sequence[str], None] = "62eece4bcbb1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_column("agents", "sdk_client_id")
    else:
        with op.batch_alter_table("agents") as batch:
            batch.drop_column("sdk_client_id")


def downgrade() -> None:
    # Nothing can reconstruct what each row's client called itself, and
    # nothing reads it any more. Backfilled with the public_key so the column
    # is non-null and, at worst, says something true: this row belongs to
    # that identity.
    if op.get_bind().dialect.name == "postgresql":
        op.add_column("agents", sa.Column("sdk_client_id", sa.String(), nullable=True))
    else:
        with op.batch_alter_table("agents") as batch:
            batch.add_column(sa.Column("sdk_client_id", sa.String(), nullable=True))
    op.execute("UPDATE agents SET sdk_client_id = public_key WHERE sdk_client_id IS NULL")
    if op.get_bind().dialect.name == "postgresql":
        op.alter_column("agents", "sdk_client_id", nullable=False)
    else:
        with op.batch_alter_table("agents") as batch:
            batch.alter_column("sdk_client_id", nullable=False)
