"""Test fixtures for souk's test suite.

Runs against a real Postgres (SOUK_DATABASE_URL, same env var souk itself
reads via souk.config.Settings) rather than sqlite/mocks — the schema uses
Postgres-specific SQL (JSONB, ON CONFLICT, make_interval), so a substitute
DB would test a different set of semantics than what actually runs. See
CONTRIBUTING.md for how to point this at a local Postgres.

Tests aren't wrapped in a rolled-back transaction: souk.repo's functions
call session.commit() internally throughout (e.g. register_agents,
create_run), so a single outer transaction can't cleanly contain a whole
test. Truncating between tests instead — cheap, and matches how the
schema is already treated (idempotent, disposable; see souk/db.py's
bootstrap_schema).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from souk.db import SessionLocal, bootstrap_schema, engine
from souk.identity import registration_signing_payload
from souk.server import app

TRUNCATE_SQL = "TRUNCATE providers, agents, threads, thread_history, run_events RESTART IDENTITY CASCADE"


@pytest.fixture(scope="session", autouse=True)
async def _schema() -> None:
    await bootstrap_schema()


@pytest.fixture(autouse=True)
async def _clean_db() -> AsyncIterator[None]:
    async with engine.begin() as conn:
        await conn.exec_driver_sql(TRUNCATE_SQL)
    yield


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as s:
        yield s


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class Identity:
    """A throwaway Ed25519 keypair plus a helper to build a signed
    /agents/register body — mirrors souk_agent_sdk.identity's
    sign/public_key_hex/registration_signing_payload exactly (souk doesn't
    depend on souk_agent_sdk, so this is reimplemented directly against
    `cryptography` rather than pulled in as a cross-project test-only
    dependency).
    """

    def __init__(self) -> None:
        self._key = Ed25519PrivateKey.generate()
        self.public_key = self._key.public_key().public_bytes_raw().hex()

    def register_body(self, sdk_client_id: str, agents: list[dict]) -> dict:
        timestamp = int(time.time())
        names = [a["name"] for a in agents]
        payload = registration_signing_payload(sdk_client_id, names, timestamp)
        return {
            "sdk_client_id": sdk_client_id,
            "public_key": self.public_key,
            "signature": self._key.sign(payload).hex(),
            "timestamp": timestamp,
            "agents": agents,
        }


@pytest.fixture
def new_identity() -> type[Identity]:
    return Identity
