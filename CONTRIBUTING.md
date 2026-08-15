# Contributing

Thanks for taking a look. This repo is a set of independent projects
sharing one git history, not one coupled monorepo — see the README's
[Components](README.md#components) table for what each one is, and read
that section before assuming a change belongs where you'd first guess.

## No shared workspace

There is deliberately no shared `uv` workspace tying `souk/`,
`souk-agent-sdk/`, `souk-client-sdk/`, `agent-template/`, and
`providers/*` together — each has its own `pyproject.toml` and is synced
independently:

```bash
cd souk && uv sync --group dev
cd souk-agent-sdk && uv sync --group dev
cd agent-template && uv sync   # path-depends on souk-agent-sdk
```

(The serving layer is not in this tree: the reference gateway lives in
[AgentSoukServer](https://github.com/hukaichun/AgentSoukServer), which
consumes `souk` through a submodule. Network-facing changes belong there
— see issue #27 for the boundary.)

## Running the tests

`souk/tests/` holds nearly all of the business logic (registration/
identity, claiming, routing, offline handling) and needs no stubs and no
web framework — souk depends on neither. What only exists once there is a
socket — the HTTP surfaces, the relay, the KYOK endpoints — is tested in
AgentSoukServer's own suite.

The suite runs against **SQLite by default**, with no database to stand up
first — souk's schema and queries are dialect-neutral (see `souk/schema.py`
and `souk/repo.py`), so it exercises the same semantics on either backend.

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

`souk-directory/` has no test suite (static TS/HTML, no backend logic to
unit test) — `npm run build` failing on a type error is its CI check.

CI (`.github/workflows/ci.yml`) runs one job per subproject and must pass
before a PR merges.

## Where a change belongs

- Domain behavior (routing, identity, run dispatch, persistence, protocol
  translation) → `souk/`.
- Anything that needs a socket — endpoints, transports, TLS, wire framing
  → the [AgentSoukServer](https://github.com/hukaichun/AgentSoukServer)
  repo, not here (issue #27).
- The agent-side polling/dispatch client, or the A2A sub-agent-calling
  helper → `souk-agent-sdk/`.
- A caller-side convenience wrapper → `souk-client-sdk/`.
- A new reference provider or framework example → `providers/` (see
  `providers/README.md`); keep `agent-template/` itself minimal, don't
  grow it into a framework.
- The human-browsable directory/chat UI → `souk-directory/`.

## Commits / PRs

Small, one logical change per commit — the existing `git log` is the best
reference for the expected granularity and message style. Open an issue
first for anything that isn't an obvious bug fix, especially anything
touching the identity/routing model or the wire frames (authored in
AgentSoukServer's `docs/server-mode.md`) — see README's
[Roadmap](README.md#roadmap) for what's already a known direction versus
what needs discussion first.
