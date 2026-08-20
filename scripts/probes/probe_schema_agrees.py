"""Does `alembic upgrade head` build the same schema as `funduq/schema.py`?

Two definitions of one schema — the Core metadata the app queries through, and
the migration a deployment actually runs — and nothing makes them agree except
whoever last edited both. `d363d76` compared them by hand when the migration
chain was collapsed; this is that check, kept.

    cd funduq && uv run python ../scripts/probes/probe_schema_agrees.py
    FUNDUQ_DATABASE_URL=postgresql+psycopg://… uv run --extra postgres python ../scripts/probes/probe_schema_agrees.py

Both backends, because this is exactly where they differ: SQLite renders JSON
as TEXT and needs `INTEGER PRIMARY KEY` for autoincrement, Postgres has JSONB
and BIGINT identity, and a migration can be right on one and wrong on the
other.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url

from funduq.schema import metadata

FUNDUQ_DIR = Path(__file__).resolve().parents[2] / "funduq"


def _sync_url(url: str) -> str:
    parsed = make_url(url)
    if parsed.get_driver_name() == "aiosqlite":
        return parsed.set(drivername="sqlite").render_as_string(hide_password=False)
    return url


def _describe(engine) -> dict:
    """Everything two schemas can differ by that funduq would notice."""
    inspector = inspect(engine)
    out: dict = {}
    for table in sorted(inspector.get_table_names()):
        if table == "alembic_version":
            continue
        out[table] = {
            "columns": {
                c["name"]: (str(c["type"]).upper(), bool(c["nullable"]))
                for c in inspector.get_columns(table)
            },
            "pk": tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
            "fks": sorted(
                (tuple(f["constrained_columns"]), f["referred_table"], tuple(f["referred_columns"]))
                for f in inspector.get_foreign_keys(table)
            ),
            "unique": sorted(
                tuple(u["column_names"]) for u in inspector.get_unique_constraints(table)
            ),
            "indexes": sorted(
                (tuple(i["column_names"]), bool(i["unique"])) for i in inspector.get_indexes(table)
            ),
        }
    return out


def main() -> int:
    configured = os.environ.get("FUNDUQ_DATABASE_URL")
    tmp = Path(tempfile.mkdtemp(prefix="funduq-schema-"))

    if configured and make_url(configured).get_backend_name() != "sqlite":
        print("Postgres: comparing in two schemas of the same database")
        base = _sync_url(configured)
        migrated_engine = create_engine(base)
        with migrated_engine.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA IF EXISTS probe_migrated CASCADE")
            conn.exec_driver_sql("DROP SCHEMA IF EXISTS probe_declared CASCADE")
            conn.exec_driver_sql("CREATE SCHEMA probe_migrated")
            conn.exec_driver_sql("CREATE SCHEMA probe_declared")
        env = {**os.environ, "FUNDUQ_DATABASE_URL": configured, "FUNDUQ_DB_SCHEMA": "probe_migrated"}
        declared_engine = create_engine(base, connect_args={"options": "-c search_path=probe_declared"})
        migrated_engine = create_engine(base, connect_args={"options": "-c search_path=probe_migrated"})
    else:
        print("SQLite: comparing two throwaway files")
        migrated_url = f"sqlite:///{tmp / 'migrated.db'}"
        declared_url = f"sqlite:///{tmp / 'declared.db'}"
        env = {**os.environ, "FUNDUQ_DATABASE_URL": migrated_url}
        migrated_engine = create_engine(migrated_url)
        declared_engine = create_engine(declared_url)

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=FUNDUQ_DIR, env=env, check=True, capture_output=True,
    )
    metadata.create_all(declared_engine)

    migrated, declared = _describe(migrated_engine), _describe(declared_engine)

    differences: list[str] = []
    for table in sorted(set(migrated) | set(declared)):
        if table not in migrated:
            differences.append(f"{table}: built by schema.py, missing from the migration")
            continue
        if table not in declared:
            differences.append(f"{table}: built by the migration, missing from schema.py")
            continue
        for aspect in ("columns", "pk", "fks", "unique", "indexes"):
            if migrated[table][aspect] != declared[table][aspect]:
                differences.append(
                    f"{table}.{aspect}:\n"
                    f"    migration: {migrated[table][aspect]}\n"
                    f"    schema.py: {declared[table][aspect]}"
                )

    print(f"\ntables compared: {sorted(set(migrated) & set(declared))}")
    if differences:
        print(f"\n{len(differences)} difference(s):\n")
        for d in differences:
            print(f"  {d}")
        return 1
    print("\nthe migration and schema.py agree, column by column")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
