"""allow cancelling status

Revision ID: 62eece4bcbb1
Revises: 9fc9cba7cf67
Create Date: 2026-08-15 03:08:36.485611

Adds 'cancelling' to thread_history's status CHECK.

souk asks a provider to stop; it cannot make it stop. Recording 'cancelled'
at request time claimed something souk did not know — a provider is free to
ignore the request and finish normally, and then the run really did complete.
'cancelling' records the fact souk *does* know (a stop was requested and the
provider still has the run), and the outcome is decided when the agent's
stream actually ends. See souk.handlers._handle_finish.

Rewriting a CHECK constraint differs by dialect in opposite ways: Postgres
drops and re-adds one in place, while SQLite cannot alter a constraint at all
and needs the table rebuilt — which alembic's batch mode does by copying into
a new table. Both paths are spelled out rather than hidden, since they really
are different operations.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "62eece4bcbb1"
down_revision: Union[str, Sequence[str], None] = "9fc9cba7cf67"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_CONSTRAINT = "ck_thread_history_status"
_OLD = "status IN ('queued', 'running', 'input-required', 'resumed', 'completed', 'failed', 'cancelled')"
_NEW = (
    "status IN ('queued', 'running', 'input-required', 'resumed', 'cancelling', "
    "'completed', 'failed', 'cancelled')"
)


def _replace_status_check(condition: str) -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(_CONSTRAINT, "thread_history", type_="check")
        op.create_check_constraint(_CONSTRAINT, "thread_history", sa.text(condition))
    else:
        with op.batch_alter_table("thread_history") as batch:
            batch.drop_constraint(_CONSTRAINT, type_="check")
            batch.create_check_constraint(_CONSTRAINT, sa.text(condition))


def upgrade() -> None:
    _replace_status_check(_NEW)


def downgrade() -> None:
    # Any row still mid-cancel would violate the narrower constraint, so
    # settle it first: souk asked it to stop and it never reported back,
    # which is the same evidence 'cancelled' is normally recorded on.
    op.execute("UPDATE thread_history SET status = 'cancelled' WHERE status = 'cancelling'")
    _replace_status_check(_OLD)
