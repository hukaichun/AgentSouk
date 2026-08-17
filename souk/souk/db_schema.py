DEFAULT_DB_SCHEMA = "public"

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./souk.db"

EXPECTED_SCHEMA_REVISION = "a3d1c47be902"


def quoted_schema(db_schema: str) -> str:
    return f'"{db_schema}"'
