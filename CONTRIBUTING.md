# Contributing

Thanks for taking a look. This repo is a set of independent projects
sharing one git history, not one coupled monorepo — see the README's
[Repository structure](README.md#repository-structure) table for what each one is, and read
that section before assuming a change belongs where you'd first guess.

## What lives here, and what doesn't

This tree is the library (`souk/`), the two provider-side contract
packages (`souk-provider-sdk/`, `souk-llm-provider-sdk/`), the published
site (`docs/`) and the working design record (`design/`).
Everything else — the gateway, the transport SDKs, the reference
providers and the directory UI — lives in
[AgentSoukServer](https://github.com/hukaichun/AgentSoukServer),
which consumes `souk` through a submodule and owns both ends of every
wire it defines — anything network-facing belongs there (see issue #27
for the boundary).

There is deliberately no shared `uv` workspace; each project (`souk`,
`souk-provider-sdk`, `souk-llm-provider-sdk`) syncs independently:

```bash
cd souk && uv sync --group dev
```

## Running the tests

`souk/tests/` holds nearly all of the business logic (registration/
identity, claiming, routing, offline handling) and needs no stubs and no
web framework — souk depends on neither. What only exists once there is a
socket — the HTTP surfaces, the relay, the KYOK endpoints — is tested in
AgentSoukServer's own suite.

The suite runs against **SQLite by default**, with no database to stand up
first — souk's schema and queries are dialect-neutral (see
`souk/souk/schema.py` and `souk/souk/repo.py`), so it exercises the same
semantics on either backend.

```bash
cd souk
uv sync --group dev
uv run pytest -v                 # SQLite, zero config
```

To run the exact same suite against Postgres, export a DSN first (the
`postgres` extra / dev group already brings in psycopg):

```bash
docker compose up paradedb -d    # or point at any local Postgres
export SOUK_DATABASE_URL=postgresql+psycopg://souk:souk@localhost:5433/souk
(cd souk && uv run pytest -v)
```

`conftest.py` supplies a throwaway SQLite file and a test signing secret
when the corresponding env vars are unset, so `pytest` works out of the
box; exporting `SOUK_DATABASE_URL` (and/or `SOUK_TOKEN_SIGNING_SECRET`)
overrides those defaults. Note the running server has no default for
`SOUK_TOKEN_SIGNING_SECRET` — it must be set explicitly to start souk (an
insecure fallback would be a real auth bypass), unlike `SOUK_DATABASE_URL`,
which defaults to a local SQLite file.

The test suite applies `souk/alembic/` itself (see `tests/conftest.py`'s
`_schema` fixture) — no separate migration step needed for tests. A real
deployment runs `uv run alembic upgrade head` before starting the server
(see AgentSoukServer's compose, whose `souk-migrate` service is exactly
that step). If you change the schema, add a new revision under
`souk/alembic/versions/` rather than editing the initial one —
`uv run alembic revision -m "..."` from `souk/`.

CI (`.github/workflows/ci.yml`) runs the `souk` suite (SQLite and
Postgres) and must pass before a PR merges.

## Where a change belongs

- Domain behavior (routing, identity, run dispatch, persistence, protocol
  translation) → `souk/`.
- Anything that needs a socket — endpoints, transports, TLS, wire framing
  — and the SDKs, reference providers and directory UI that speak them →
  the [AgentSoukServer](https://github.com/hukaichun/AgentSoukServer)
  repo, not here (issue #27).

## Commits / PRs

Small, one logical change per commit — the existing `git log` is the best
reference for the expected granularity and message style. Open an issue
first for anything that isn't an obvious bug fix, especially anything
touching the identity/routing model or the wire frames (authored in
AgentSoukServer's `docs/server-mode.md`) — see README's
[Roadmap](README.md#roadmap) for what's already a known direction versus
what needs discussion first.
