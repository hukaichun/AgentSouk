# Persistence

Part of [core components](../core-components.md).

What souk stores, one line per table:

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
(`souk/alembic`), and `souk.migrate()` (or `python -m souk.migrate`)
runs it programmatically — a fresh database is created at head, an old
one upgrades in place, and the version row that `health()` checks is
written by the same mechanism. There is deliberately no second
create-the-tables path to drift against. Deployments that manage schema
themselves have three documented tiers, down to "no Alembic at all":
make the tables match the published metadata and write the one expected
revision row.
