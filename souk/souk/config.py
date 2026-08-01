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


settings = Settings()
