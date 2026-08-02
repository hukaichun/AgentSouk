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
routing, offline handling). It needs a real Postgres, not a mock or
sqlite — the schema leans on Postgres-specific SQL (JSONB, `ON CONFLICT`,
`make_interval`), so a substitute would exercise different semantics than
what actually runs.

```bash
docker compose up paradedb -d   # or point at any local Postgres
cd souk
uv sync --group dev
uv run bash ../scripts/gen_proto.sh souk/grpc_gen
SOUK_DATABASE_URL=postgresql+psycopg://souk:souk@localhost:5433/souk uv run pytest -v
```

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
