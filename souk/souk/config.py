from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SOUK_")

    database_url: str = "postgresql+psycopg://souk:souk@localhost:5432/souk"
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    grpc_host: str = "0.0.0.0"
    grpc_port: int = 50051
    online_window_seconds: int = 60
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

    # Signs the bearer tokens issued at /agents/register and required on
    # every gRPC call (see souk.identity) — MUST be overridden with a real
    # secret in any deployment reachable by anyone other than yourself;
    # the default is only safe for a single-developer local souk.
    token_signing_secret: str = "dev-insecure-change-me"

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
