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

If you touch `proto/souk.proto`, regenerate both packages' gRPC stubs from
the repo root before doing anything else — see README's
[Regenerating gRPC stubs](README.md#regenerating-grpc-stubs):

```bash
uv sync --group dev
uv run bash scripts/gen_proto.sh
```

## Running the tests

`souk/` is the only subproject with a test suite so far (`souk/tests/`) —
it holds nearly all of the actual business logic (registration/identity,
routing, offline handling). It runs against **SQLite by default**, with no
database to stand up first — souk's schema and queries are dialect-neutral
(see `souk/schema.py` and `souk/repo.py`), so the same suite exercises the
same semantics on either backend.

```bash
cd souk
uv sync --group dev
uv run bash ../scripts/gen_proto.sh souk/grpc_gen
uv run pytest -v                 # SQLite, zero config
```

To run the exact same suite against Postgres, export a DSN first (the
`postgres` extra / dev group already brings in psycopg):

```bash
docker compose up paradedb -d    # or point at any local Postgres
SOUK_DATABASE_URL=postgresql+psycopg://souk:souk@localhost:5433/souk \
uv run pytest -v
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
(see `docker-compose.yml`'s `souk-migrate` service). If you change the
schema, add a new revision under `souk/alembic/versions/` rather than
editing the initial one — `uv run alembic revision -m "..."` from `souk/`.

`souk-directory/` has no test suite (static TS/HTML, no backend logic to
unit test) — `npm run build` failing on a type error is its CI check.

CI (`.github/workflows/ci.yml`) runs one job per subproject and must pass
before a PR merges.

## Where a change belongs

- Gateway/relay behavior (routing, identity, run dispatch, persistence) →
  `souk/`.
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
touching the identity/routing model or `proto/souk.proto` — see README's
[Roadmap](README.md#roadmap) for what's already a known direction versus
what needs discussion first.
