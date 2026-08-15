"""Serving configuration: everything that only exists because souk is being
exposed on a network.

Deliberately a separate class in a separate distribution from
`souk.config.CoreSettings`. Core never reads any of this — it cannot, it does
not depend on this package — which is what makes "core knows a database and
nothing else" a property of the packaging rather than of everyone's
discipline. Both are still `pydantic-settings` models reading the same
`SOUK_*` environment variables, so a deployment configures one process the
way it always did.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class ServingSettings(BaseSettings):
    """Everything that only exists because souk is being exposed on a
    network. Consumed by whoever actually binds the sockets —
    souk_server.server. Core never reads any of this, and since the split it
    could not: it does not depend on this package.
    """

    model_config = SettingsConfigDict(env_prefix="SOUK_")

    http_host: str = "0.0.0.0"
    http_port: int = 8000
    grpc_host: str = "0.0.0.0"
    grpc_port: int = 50051

    # Origins allowed to call souk's HTTP surface cross-origin (e.g. a
    # souk-directory instance served from a different origin). "*" is fine
    # for local development; tighten this for any real deployment, same as
    # token_signing_secret's default is only safe for a single-developer
    # local souk.
    cors_allow_origins: list[str] = ["*"]

    # Base URL callers use to reach this souk's HTTP surface, used to build
    # per-agent Agent Card URLs. Override in deployments behind a proxy/LB.
    # Deliberately not a core setting even though it ends up in protocol
    # content: core should not know what it is called on a network, so
    # whoever serves souk passes this to the protocol layer.
    public_http_url: str = "http://localhost:8000"

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
