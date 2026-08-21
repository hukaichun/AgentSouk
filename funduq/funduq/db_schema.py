DEFAULT_DB_SCHEMA = "public"

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./funduq.db"

EXPECTED_SCHEMA_REVISION = "a11c3b7d42e9"


def quoted_schema(db_schema: str) -> str:
    return f'"{db_schema}"'
