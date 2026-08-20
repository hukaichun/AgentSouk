DEFAULT_DB_SCHEMA = "public"

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./souk.db"

EXPECTED_SCHEMA_REVISION = "ff342e6c6b85"


def quoted_schema(db_schema: str) -> str:
    return f'"{db_schema}"'
