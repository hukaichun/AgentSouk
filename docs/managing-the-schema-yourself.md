# Managing the schema yourself

`souk.migrate()` is the convenience entry, not a mandatory channel. The
packaged Alembic chain (`souk/alembic`, inside the wheel) is the authority
on what the schema *is*; who applies it, when, and with which credentials
is yours. Three tiers, by how much control you want.

## Tier 1 — you run the chain, on your terms

Point your own `alembic.ini` at the packaged chain:

```ini
[alembic]
script_location = souk:alembic
```

and run `alembic upgrade head` whenever and however your process demands —
typically with a DDL-capable role for this step while the running service
holds a DML-only role. This is exactly what this repo's own `alembic.ini`
does; a pip install changes nothing about it.

## Tier 2 — souk writes the SQL, your DBA applies it

Offline mode emits the full DDL script without touching any database:

```bash
SOUK_DATABASE_URL="postgresql+psycopg://…" alembic upgrade head --sql > souk-schema.sql
```

Hand the file to whoever owns change management. The script includes the
one line souk itself will later check for —
`INSERT INTO alembic_version (version_num) VALUES ('…')` — so a database
built this way passes `health()` with nothing else to do. For a version
bump, `alembic upgrade <from>:<to> --sql` emits the increment the same way.

## Tier 3 — no Alembic at all

If schema management belongs to some other tool entirely (Flyway,
hand-reviewed SQL, anything), souk's whole contract with the database is
two public facts:

1. the tables match `souk.schema.metadata` — an importable SQLAlchemy
   `MetaData` you can feed to any DDL generator for your dialect;
2. the `alembic_version` table holds one row equal to
   `souk.db_schema.EXPECTED_SCHEMA_REVISION` — the only thing `health()`
   reads to tell a migrated database from one nobody prepared.

Satisfy both and souk does not care who built the tables. The revision
constant moves when the schema does, so a deployment pinned this way
notices a mismatched upgrade at health-check time, not at first query.
