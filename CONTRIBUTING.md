# Contributing

Thanks for taking a look. This repo is a set of independent projects
sharing one git history, not one coupled monorepo — see the README's
[Repository structure](README.md#repository-structure) table for what each one is, and read
that section before assuming a change belongs where you'd first guess.

## What lives here, and what doesn't

This tree is the library (`funduq/`), the two provider-side contract
packages (`funduq-provider-sdk/`, `funduq-llm-provider-sdk/`) and the published
site (`docs/`), which carries the design records too.
Everything else — the gateway, the transport SDKs, the reference
providers and the directory UI — lives in
[funduq-server](https://github.com/hukaichun/funduq-server),
which consumes `funduq` through a submodule and owns both ends of every
wire it defines — anything network-facing belongs there (see issue #27
for the boundary).

There is deliberately no shared `uv` workspace; each project (`funduq`,
`funduq-provider-sdk`, `funduq-llm-provider-sdk`) syncs independently:

```bash
cd funduq && uv sync --group dev
```

## Running the tests

`funduq/tests/` holds nearly all of the business logic (registration/
identity, claiming, routing, offline handling) and needs no stubs and no
web framework — funduq depends on neither. What only exists once there is a
socket — the HTTP surfaces, the relay, the KYOK endpoints — is tested in
funduq-server's own suite.

The suite runs against **SQLite by default**, with no database to stand up
first — funduq's schema and queries are dialect-neutral (see
`funduq/funduq/schema.py` and `funduq/funduq/repo.py`), so it exercises the same
semantics on either backend.

```bash
cd funduq
uv sync --group dev
uv run pytest -v                 # SQLite, zero config
```

To run the exact same suite against Postgres, export a DSN first (the
`postgres` extra / dev group already brings in psycopg):

```bash
docker compose up paradedb -d    # or point at any local Postgres
export FUNDUQ_DATABASE_URL=postgresql+psycopg://funduq:funduq@localhost:5433/funduq
(cd funduq && uv run pytest -v)
```

`conftest.py` supplies a throwaway SQLite file and a test signing secret
when the corresponding env vars are unset, so `pytest` works out of the
box; exporting `FUNDUQ_DATABASE_URL` (and/or `FUNDUQ_TOKEN_SIGNING_SECRET`)
overrides those defaults. Note the running server has no default for
`FUNDUQ_TOKEN_SIGNING_SECRET` — it must be set explicitly to start funduq (an
insecure fallback would be a real auth bypass), unlike `FUNDUQ_DATABASE_URL`,
which defaults to a local SQLite file.

The test suite applies `funduq/alembic/` itself (see `tests/conftest.py`'s
`_schema` fixture) — no separate migration step needed for tests. A real
deployment runs `uv run alembic upgrade head` before starting the server
(see funduq-server's compose, whose `funduq-migrate` service is exactly
that step). If you change the schema, add a new revision under
`funduq/alembic/versions/` rather than editing the initial one —
`uv run alembic revision -m "..."` from `funduq/`.

CI (`.github/workflows/ci.yml`) runs the `funduq` suite (SQLite and
Postgres) and must pass before a PR merges.

## Where a change belongs

- Domain behavior (routing, identity, run dispatch, persistence, protocol
  translation) → `funduq/`.
- Anything that needs a socket — endpoints, transports, TLS, wire framing
  — and the SDKs, reference providers and directory UI that speak them →
  the [funduq-server](https://github.com/hukaichun/funduq-server)
  repo, not here (issue #27).

## Commits / PRs

Small, one logical change per commit — the existing `git log` is the best
reference for the expected granularity and message style. Open an issue
first for anything that isn't an obvious bug fix, especially anything
touching the identity/routing model or the wire frames (authored in
funduq-server's `docs/server-mode.md`) — see README's
[Roadmap](README.md#roadmap) for what's already a known direction versus
what needs discussion first.
