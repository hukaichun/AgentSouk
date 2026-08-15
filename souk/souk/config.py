"""souk's configuration: what a network-free souk needs.

The database, the domain's own timing policy, and the key it signs its
tokens with. Everything that only means something once there is a socket —
hosts, ports, TLS, CORS — lives in `souk_server.config.ServingSettings`, in
the other distribution, which is what stops it drifting back in here. See
docs/library-architecture.md.

Neither class is instantiated at import time. A `Souk` is constructed with a
`CoreSettings` (see souk/core.py), so nothing here runs as a side effect of
importing souk, and several souks with different configuration can coexist in
one process. Both classes are still `pydantic-settings` models reading the
same `SOUK_*` environment variables, so passing settings explicitly *adds* a
way to configure souk rather than replacing the existing one: any field not
passed is still resolved from the environment — just at construction time
rather than at import time.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

from souk.db_schema import DEFAULT_DATABASE_URL, DEFAULT_DB_SCHEMA


class CoreSettings(BaseSettings):
    """Everything souk needs to run as a library, with no network at all."""

    model_config = SettingsConfigDict(env_prefix="SOUK_")

    # ---- Database

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
    # SQLite database_url (see souk/core.py). "public" — Postgres's own
    # default — is a fine default here; getting it wrong just means "wrong
    # namespace", not a silent security hole.
    db_schema: str = DEFAULT_DB_SCHEMA

    # ---- Timing policy: how souk judges an agent's or a run's state. None
    # of these describe a network; they are domain rules.

    online_window_seconds: int = 60
    # A much longer cutoff than online_window_seconds: an agent whose
    # last_seen_at is older than this is excluded from the roster
    # entirely (not just marked offline) — read-time filter only, no job,
    # no mutation; reappears automatically the moment it registers again.
    stale_hidden_window_seconds: int = 60 * 60 * 24 * 7

    # How long a run may sit 'queued' (never claimed) before souk gives up
    # on it — only applies once the target agent is also no longer online,
    # see repo.fail_unclaimed_runs. Shorter than run_stall_timeout_seconds:
    # waiting to be claimed should time out faster than "claimed and
    # stalled".
    queued_timeout_seconds: int = 45

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

    # How an in-process worker (souk/worker.py) paces its claim loop. Both
    # are domain timing, not network settings: a worker hosted in this
    # process claims over the same `claim_work` a remote one calls, so it
    # needs the same two numbers the remote SDK configures for itself.
    #
    # The long poll is how long an *idle* worker's claim call blocks waiting
    # for work before coming round again — it returns the moment work is
    # enqueued (see RunBroker.subscribe_wake), so this is only the ceiling on
    # an idle cycle, not on latency. Kept comfortably under
    # online_window_seconds: claiming is also what marks these agents seen,
    # so a worker that went a whole online window without claiming would look
    # offline while sitting right there.
    worker_long_poll_seconds: float = 25.0
    # How often a *busy* worker checks for more work. It can't long-poll then
    # — what changes while it's busy is its own capacity, which souk can't
    # observe — so this is a plain interval between top-up claims.
    worker_poll_interval_seconds: float = 2.0

    # ---- Identity

    # Signs the bearer tokens issued at registration and required on every
    # gRPC call (see souk.identity), and the run-scoped KYOK tokens (see
    # souk.kyok). Core rather than serving: issuing a token is part of
    # registering an agent, a domain act, not part of serving a port. No
    # default — an insecure fallback here is a real auth bypass (a
    # predictable/well-known signing key), not just a wrong-connection
    # nuisance, so this must always be set explicitly, via the
    # SOUK_TOKEN_SIGNING_SECRET environment variable or the constructor.
    token_signing_secret: str
