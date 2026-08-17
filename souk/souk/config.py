"""souk's configuration: what a network-free souk needs.

The database, the domain's own timing policy, and the key it signs its
tokens with. Everything that only means something once there is a socket —
hosts, ports, TLS, CORS — lives in the gateway's own `ServingSettings`, in
the AgentSoukServer repository, which is what stops it drifting back in
here. See docs/library-architecture.md.

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

    # An agent whose last_seen_at is older than this is excluded from the
    # roster entirely — read-time filter only, no job, no mutation; it
    # reappears the moment it registers again.
    #
    # There was an `online_window_seconds` beside this one, and `online` was
    # derived from it. It is gone: a provider used to prove it was there by
    # asking for work, and it does not ask any more, so the timestamp stopped
    # producing that fact. Reachability comes from the broker now (see
    # `RunBroker.serving`). This window survives because it answers a
    # different question — not "is it there" but "has it been away so long
    # that listing it is noise".
    stale_hidden_window_seconds: int = 60 * 60 * 24 * 7


    # A run past this many seconds without any activity while still
    # 'running' is presumed stalled: a provider acked it and went silent,
    # which is a real anomaly. A run merely sitting 'queued' is not — it
    # means nobody has taken it yet, and the broker gives up on those itself
    # (see RunBroker.queued_timeout_seconds). Set well above realistic
    # single-run latency; LLM calls can legitimately take a while.
    run_stall_timeout_seconds: int = 120
    health_sweep_interval_seconds: int = 15

    # How long a run may sit 'input-required' (paused, waiting on a human
    # to resolve an interrupt — see souk.pause) before souk gives up on it.
    # Unlike run_stall_timeout_seconds, a pause is
    # waiting on a person, not a provider, so there's no generally-correct
    # default duration — None (the default) means no timeout at all,
    # matching souk's original behavior before this setting existed. Set
    # this only if your deployment wants paused runs to eventually give up.
    paused_timeout_seconds: int | None = None


    # ---- Identity

    # Signs the bearer tokens issued at registration and required on every
    # worker call (see souk.identity), and the run-scoped KYOK tokens (see
    # souk.kyok). Core rather than serving: issuing a token is part of
    # registering an agent, a domain act, not part of serving a port. No
    # default — an insecure fallback here is a real auth bypass (a
    # predictable/well-known signing key), not just a wrong-connection
    # nuisance, so this must always be set explicitly, via the
    # SOUK_TOKEN_SIGNING_SECRET environment variable or the constructor.
    token_signing_secret: str

    # This souk's own Ed25519 private key, hex-encoded (32-byte seed, so 64
    # hex characters). What lets a provider tell one souk from another.
    #
    # Everything else here is one-directional: a provider proves who it is and
    # souk proves nothing back, so a provider connects to a URL and trusts
    # whatever answers. TLS does not close that — it authenticates a
    # *hostname*, and in an enterprise it routinely terminates at an
    # intercepting proxy whose CA the endpoints already trust. A key souk
    # holds is checkable without trusting any of that.
    #
    # **Optional, and a key that is absent is not a key that is generated.**
    # An ephemeral one would change on every restart, so every provider that
    # pinned it would fail on reconnect — which teaches people to click
    # through the warning, destroying the only thing pinning is worth. Unset
    # means souk simply cannot prove itself, which is today's behaviour and an
    # honest state to be in.
    #
    # A value rather than a path, for the same reason
    # `token_signing_secret` is: every replica of one souk must present the
    # same identity, or a provider sees a different one depending on which it
    # reaches, and it has to survive restarts. That makes it something to
    # provision, like any other secret — not something a process creates for
    # itself.
    identity_private_key: str | None = None
