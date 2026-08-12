"""Test fixtures for souk's test suite.

Runs against a real Postgres (SOUK_DATABASE_URL, same env var souk itself
reads via souk.config.Settings) rather than sqlite/mocks — the schema uses
Postgres-specific SQL (JSONB, ON CONFLICT, make_interval), so a substitute
DB would test a different set of semantics than what actually runs. See
CONTRIBUTING.md for how to point this at a local Postgres.

Tests aren't wrapped in a rolled-back transaction: souk.repo's functions
call session.commit() internally throughout (e.g. register_agents,
create_run), so a single outer transaction can't cleanly contain a whole
test. Truncating between tests instead — cheap, and the schema itself is
applied once per session via Alembic (see souk/alembic/), the same
`alembic upgrade head` a real deployment runs before starting souk.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator
from pathlib import Path

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from souk.db import SessionLocal, engine
from souk.identity import registration_signing_payload
from souk.server import app

TRUNCATE_SQL = "TRUNCATE providers, agents, threads, thread_history, run_events RESTART IDENTITY CASCADE"

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    command.upgrade(Config(str(ALEMBIC_INI)), "head")


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

    def sign_chain_hop(self, subject: dict, prev_token: str | None = None, exp_offset: int = 300) -> str:
        """Mirrors souk_agent_sdk.identity._sign_hop exactly (see that
        module's docstring) — reimplemented here for the same reason
        register_body reimplements the registration signing helper: souk's
        own test suite doesn't depend on souk_agent_sdk as a package.
        `exp_offset` can be negative to build an already-expired hop, for
        testing souk.identity.verify_actor_chain's per-hop exp handling.
        """
        now = int(time.time())
        payload = {
            "subject": subject,
            "actorPublicKey": self.public_key,
            "prevHash": hashlib.sha256(prev_token.encode()).hexdigest() if prev_token is not None else None,
            "iat": now,
            "exp": now + exp_offset,
        }
        return jwt.encode(payload, self._key, algorithm="EdDSA")

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
