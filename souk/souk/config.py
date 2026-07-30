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


settings = Settings()
