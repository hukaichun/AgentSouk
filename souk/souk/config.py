from pydantic_settings import BaseSettings, SettingsConfigDict

from souk.db_schema import DEFAULT_DATABASE_URL, DEFAULT_DB_SCHEMA


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOUK_")

    # Defaults to a local SQLite file so a fresh checkout, an embedding
    # library caller, or a test run works with zero configuration — no
    # Postgres to stand up first. SQLite is a genuine, supported backend
    # (souk's schema and queries are dialect-neutral, see souk/schema.py
    # and souk/repo.py), but its single-writer locking suits dev, CI, and
    # low-concurrency single-node use — not a busy multi-writer gateway.
    # For any real deployment set SOUK_DATABASE_URL to a Postgres DSN, e.g.
    # `postgresql+psycopg://user:pass@host:5432/souk`; the docs make this
    # loud precisely because the failure mode of forgetting is quiet
    # (writes land in a local file instead of the shared database).
    database_url: str = DEFAULT_DATABASE_URL
    # Postgres schema souk's own tables live under (see souk/alembic/env.py,
    # which creates it and points search_path at it). Lets a deployment
    # sharing one Postgres instance across services keep souk's objects out
    # of `public` without hand-editing search_path on the DB role. Postgres
    # only — SQLite has no schema namespace, so this setting is ignored on a
    # SQLite database_url (see souk/db.py). "public" — Postgres's own
    # default — is a fine default here; getting it wrong just means "wrong
    # namespace", not a silent security hole.
    db_schema: str = DEFAULT_DB_SCHEMA
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    grpc_host: str = "0.0.0.0"
    grpc_port: int = 50051
    online_window_seconds: int = 60
    # A much longer cutoff than online_window_seconds: an agent whose
    # last_seen_at is older than this is excluded from GET /agents
    # entirely (not just marked offline) — read-time filter only, no job,
    # no mutation; reappears automatically the moment it registers again.
    stale_hidden_window_seconds: int = 60 * 60 * 24 * 7

    # How long a run may sit 'queued' (never claimed) before souk gives up
    # on it — only applies once the target agent is also no longer online,
    # see repo.fail_unclaimed_runs. Shorter than run_stall_timeout_seconds:
    # waiting to be claimed should time out faster than "claimed and
    # stalled".
    queued_timeout_seconds: int = 45

    # Origins allowed to call souk's HTTP surface cross-origin (e.g. a
    # souk-directory instance served from a different origin). "*" is fine
    # for local development; tighten this for any real deployment, same as
    # token_signing_secret's default is only safe for a single-developer
    # local souk.
    cors_allow_origins: list[str] = ["*"]
    # Base URL callers use to reach this souk's HTTP surface, used to build
    # per-agent Agent Card URLs. Override in deployments behind a proxy/LB.
    public_http_url: str = "http://localhost:8000"

    # A run past this many seconds without any activity (claimed, or an
    # event relayed) while still 'running' is presumed stalled — the
    # provider claimed it and went silent, a real anomaly (as opposed to
    # a run merely sitting 'queued', which just means the provider hasn't
    # claimed it yet and isn't itself a health signal — see PollRequest.
    # max_claim). Set well above realistic single-run latency (LLM calls
    # can legitimately take a while).
    run_stall_timeout_seconds: int = 120
    health_sweep_interval_seconds: int = 15

    # How long a run may sit 'input-required' (paused, waiting on a human
    # to resolve an interrupt — see souk.pause) before souk gives up on it.
    # Unlike run_stall_timeout_seconds/queued_timeout_seconds, a pause is
    # waiting on a person, not a provider, so there's no generally-correct
    # default duration — None (the default) means no timeout at all,
    # matching souk's original behavior before this setting existed. Set
    # this only if your deployment wants paused runs to eventually give up.
    paused_timeout_seconds: int | None = None

    # Signs the bearer tokens issued at /agents/register and required on
    # every gRPC call (see souk.identity). No default — an insecure
    # fallback here is a real auth bypass (a predictable/well-known
    # signing key), not just a wrong-connection nuisance, so this must
    # always be set explicitly via SOUK_TOKEN_SIGNING_SECRET.
    token_signing_secret: str

    # TLS for the gRPC server (PollForWork/AgentSession) and the HTTP
    # server (/agents/register, /agui/*, /a2a/*). Both left unset means
    # plaintext — fine for same-host development, never for a souk
    # reachable over a real network: without TLS, session tokens and
    # signed requests are visible to anyone on the path, and a captured
    # registration signature is only bounded by
    # souk.identity.SIGNATURE_FRESHNESS_WINDOW_SECONDS (60s), not
    # prevented outright. See scripts/gen_dev_tls_cert.py for a
    # self-signed pair to test with; use a real CA-issued cert (or
    # terminate TLS at a reverse proxy in front of souk) for anything
    # else.
    grpc_tls_cert_path: str | None = None
    grpc_tls_key_path: str | None = None
    http_tls_cert_path: str | None = None
    http_tls_key_path: str | None = None


settings = Settings()
