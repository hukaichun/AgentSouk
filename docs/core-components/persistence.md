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
| `alembic_version` | the schema revision `health()` checks against `EXPECTED_SCHEMA_REVISION` |

All access goes through `repo.py` — the single reader/writer — against the
SQLAlchemy metadata in `schema.py`. One code path serves both dialects
(SQLite is the zero-config default; Postgres is an extra plus a DSN), and
new database code must not reintroduce dialect-specific SQL.

The migration chain ships inside the package (`souk/alembic`), behind
`souk.migrate()` / `python -m souk.migrate`: a fresh database and a
version bump are the same operation, upgrade to head. Deployments that
manage schema themselves have three documented tiers — own credentials,
DBA-applied SQL emitted offline, or no Alembic at all against souk's
two-fact contract (tables match `souk.schema.metadata`; one
`alembic_version` row matches the expected revision).
