"""providers holds every identity, with a fingerprint

Revision ID: c4d7e2a91f38
Revises: a3f1c07d2b94
Create Date: 2026-08-15 17:20:00.000000

A provider's public key is 64 hex characters, which is unusable as an
address a person reads or types. `fingerprint` is the short derived form
(souk.identity.provider_fingerprint), UNIQUE so two identities can never
share an address — enforced by the index rather than by a check in Python,
which two concurrent registrations could both pass.

Two shape changes come with it:

- `providers` gains a row per registered identity, not just per identity
  that chose a display name. Resolving a short address has to be able to
  miss for a key that exists but never named itself, which a table of
  labels cannot answer.
- `display_name` becomes nullable, since those new rows have nothing to put
  in it. NULL means "never said".

The backfill computes fingerprints in Python: SQLite has no sha256, and
doing it in the application is also the only way to be sure both backends
produce exactly what `provider_fingerprint` would. Keys come from `agents`
(the authoritative record of who has registered) unioned with any existing
`providers` rows.

If two already-registered keys collide on their fingerprint, this migration
stops rather than dropping one: it is not a case that should be resolved
silently, and at 64 bits it means someone arranged it.
"""

import hashlib
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d7e2a91f38"
down_revision: Union[str, Sequence[str], None] = "a3f1c07d2b94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FINGERPRINT_HEX_LENGTH = 16


def _fingerprint(public_key: str) -> str:
    # Deliberately duplicated from souk.identity rather than imported: a
    # migration is a record of what was done at the time, and must keep
    # producing the same bytes even if the application's version changes.
    return hashlib.sha256(bytes.fromhex(public_key)).hexdigest()[:_FINGERPRINT_HEX_LENGTH]


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # Nullable first: the rows that exist have no value yet, and the ones
    # that don't exist have to be inserted before anything can be NOT NULL.
    if is_postgres:
        op.add_column("providers", sa.Column("fingerprint", sa.String(), nullable=True))
        op.alter_column("providers", "display_name", existing_type=sa.String(), nullable=True)
    else:
        with op.batch_alter_table("providers") as batch:
            batch.add_column(sa.Column("fingerprint", sa.String(), nullable=True))
            batch.alter_column("display_name", existing_type=sa.String(), nullable=True)

    existing = {row[0] for row in bind.execute(sa.text("SELECT public_key FROM providers"))}
    keys = {row[0] for row in bind.execute(sa.text("SELECT DISTINCT public_key FROM agents"))}
    keys |= existing

    seen: dict[str, str] = {}
    for key in sorted(keys):
        fingerprint = _fingerprint(key)
        if fingerprint in seen:
            raise RuntimeError(
                f"cannot migrate: {key} and {seen[fingerprint]} share the fingerprint "
                f"{fingerprint}. Two identities cannot share an address, and which one "
                "keeps it is not this migration's call to make."
            )
        seen[fingerprint] = key

    # Which keys need a row is decided here rather than with an
    # INSERT ... WHERE NOT EXISTS: that puts one parameter in two roles (a
    # selected value and a comparison against a varchar column) and Postgres
    # refuses to infer a single type for it. It only shows up with rows to
    # insert, so an empty-database run passes on both backends.
    for fingerprint, key in seen.items():
        if key in existing:
            bind.execute(
                sa.text("UPDATE providers SET fingerprint = :fp WHERE public_key = :pk"),
                {"fp": fingerprint, "pk": key},
            )
        else:
            bind.execute(
                sa.text(
                    "INSERT INTO providers (public_key, fingerprint, display_name, updated_at) "
                    "VALUES (:pk, :fp, NULL, :now)"
                ),
                {"pk": key, "fp": fingerprint, "now": _now_value(bind)},
            )

    if is_postgres:
        op.alter_column("providers", "fingerprint", existing_type=sa.String(), nullable=False)
        op.create_unique_constraint("uq_providers_fingerprint", "providers", ["fingerprint"])
    else:
        with op.batch_alter_table("providers") as batch:
            batch.alter_column("fingerprint", existing_type=sa.String(), nullable=False)
            batch.create_unique_constraint("uq_providers_fingerprint", ["fingerprint"])


def _now_value(bind):
    """A UTC timestamp bound as a parameter, the same way repo.py writes them
    — rather than a SQL now(), which differs by dialect and by timezone."""
    from datetime import datetime, timezone

    value = datetime.now(timezone.utc)
    return value if bind.dialect.name == "postgresql" else value.isoformat(sep=" ")


def downgrade() -> None:
    # The rows this migration inserted (identities that never named
    # themselves) are indistinguishable from any other row with a NULL name,
    # and dropping them would lose nothing the old schema could hold anyway —
    # so they go, and display_name goes back to NOT NULL over what remains.
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM providers WHERE display_name IS NULL"))
    if bind.dialect.name == "postgresql":
        op.drop_constraint("uq_providers_fingerprint", "providers", type_="unique")
        op.drop_column("providers", "fingerprint")
        op.alter_column("providers", "display_name", existing_type=sa.String(), nullable=False)
    else:
        with op.batch_alter_table("providers") as batch:
            batch.drop_constraint("uq_providers_fingerprint", type_="unique")
            batch.drop_column("fingerprint")
            batch.alter_column("display_name", existing_type=sa.String(), nullable=False)
