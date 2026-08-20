# Persistence

Part of [core components](../core-components.md).

What funduq stores, one line per table:

| table | holds |
|---|---|
| `providers` | one row per identity ever seen: public key, fingerprint, optional display name |
| `agents` | registered agents: `(provider_key, name)`, agent card, joined/last-seen timestamps |
| `llm_providers` | registered LLM offerings: `(provider_key, name)`, metadata, timestamps |
| `threads` | conversation containers: id, owning agent, parent-thread lineage, metadata |
| `runs` | one run per row: status, the AG-UI input it was dispatched with, metadata |
| `run_events` | the ordered AG-UI event log of each run, as relayed |
| `thread_messages` | the folded message history a thread reads back |
| `alembic_version` | the schema revision `health()` checks against the expected one |

## How it is implemented

The tables are declared once as SQLAlchemy **Core** metadata (`schema.py`)
— table objects and typed columns, no ORM classes, no lazy loading.
Free-form content (agent cards, metadata, event payloads, run input) is
stored in JSON columns; identities and names are plain strings;
timestamps are UTC.

Every read and write goes through one module of async functions
(`repo.py`): each function takes an open session, builds a Core
statement (`select`/`insert`/`update`/`delete`), and the writing ones
commit before returning. Registration is an upsert — registering a name
that exists updates its card and `last_seen_at` rather than erroring —
and attach refreshes `last_seen_at` for the names it serves, which is
what the roster listings use to hide stale entries.

Dialect neutrality is structural, not disciplined: because everything is
built from the shared metadata and Core expressions, the same statements
compile for SQLite (the zero-config default — an on-disk file, async via
`aiosqlite`) and Postgres (an extra plus a DSN). The test suite runs
against both backends; dialect-specific SQL is not accepted into this
layer.

Schema lifecycle: the Alembic chain ships **inside the package**
(`funduq/alembic` in the installed wheel), and
`funduq.migrate(database_url=None, db_schema=None)` — or
`python -m funduq.migrate` — runs it programmatically. A fresh database is
created at head, an old one upgrades in place, and the version row is
written by the same mechanism. There is deliberately no second
create-the-tables path to drift against.

## Managing the schema yourself

Plenty of deployments will not let an application migrate its own
database. funduq does not require it to. Three tiers, each smaller than
the last, and all of them supported rather than tolerated.

**Tier 1 — you run the chain, on your terms.** Point your own
`alembic.ini` at the packaged chain:

```ini
[alembic]
script_location = funduq:alembic
```

That single line is what this repository's own `alembic.ini` uses, so
you are running exactly what funduq runs, on your schedule and under your
review.

**Tier 2 — funduq writes the SQL, your DBA applies it.** Alembic's offline
mode emits the DDL without contacting a database:

```bash
alembic upgrade head --sql
```

Run it from `funduq/` — that is where `alembic.ini` lives, not the
repository root. The output ends by stamping the version row itself, so a DBA who applies
the script has a database funduq recognizes without funduq ever holding
credentials. `alembic upgrade <from>:<to> --sql` narrows it to one step
when you are upgrading in place.

**Tier 3 — no Alembic at all.** Make the tables match, then tell funduq
which revision they match. Two facts, both importable:

- `funduq.schema.metadata` — the SQLAlchemy `MetaData` every table is
  declared in, and the same object the migration chain targets. Whatever
  builds your schema must produce these tables and columns.
- `funduq.db_schema.EXPECTED_SCHEMA_REVISION` — the one string to write
  into `alembic_version`. A test asserts it equals the chain's head, so
  it cannot drift from the migrations.

!!! note "`health()` checks the revision you wrote"
    `Funduq.health()` returns both the revision it found and the one funduq
    expected, and the `Health` it returns compares them:
    `schema_current` is that comparison, and `ready` requires it. So a
    Tier-3 database with the wrong revision row reports not-ready rather
    than failing later in some unrelated place. (Note also that
    `Funduq.health()` — the readiness probe — is a different thing from
    the `health` module, which runs the stalled-run sweeps.)
