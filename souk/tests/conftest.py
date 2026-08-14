"""Test fixtures for souk's test suite.

Runs against SQLite by default — zero configuration, no database to stand
up first. The same suite runs against Postgres by exporting SOUK_DATABASE_URL
(a `postgresql+psycopg://…` DSN) before invoking pytest; souk's schema and
queries are dialect-neutral (see souk/schema.py, souk/repo.py), so both
backends exercise the same semantics. See CONTRIBUTING.md for the Postgres
setup.

Tests aren't wrapped in a rolled-back transaction: souk.repo's functions
call session.commit() internally throughout (e.g. register_agents,
create_run), so a single outer transaction can't cleanly contain a whole
test. The schema is applied once per session via Alembic (the same
`alembic upgrade head` a real deployment runs), and rows are cleared
between tests.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from collections.abc import AsyncIterator
from pathlib import Path

# Set before importing any souk module: souk.config.Settings loads at import
# time (souk.db / souk.server pull it in below), so the backend URL and the
# required signing secret must already be in the environment. Both use
# setdefault, so exporting SOUK_DATABASE_URL (e.g. a Postgres DSN) before
# pytest still wins — this only supplies the zero-config SQLite default.
_DEFAULT_TEST_DB = Path(tempfile.gettempdir()) / "souk_pytest.db"
os.environ.setdefault("SOUK_DATABASE_URL", f"sqlite+aiosqlite:///{_DEFAULT_TEST_DB}")
os.environ.setdefault("SOUK_TOKEN_SIGNING_SECRET", "test-signing-secret")

import jwt  # noqa: E402
import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from souk.db import SessionLocal, engine  # noqa: E402
from souk.identity import registration_signing_payload  # noqa: E402
from souk.server import app  # noqa: E402

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

# FK-safe teardown order (children before parents) for the SQLite path,
# where there's no TRUNCATE ... CASCADE. Postgres uses TRUNCATE directly.
_TABLES_CHILD_FIRST = ("run_events", "thread_history", "threads", "agents", "providers")

_IS_POSTGRES = engine.sync_engine.dialect.name == "postgresql"


@pytest.fixture(scope="session", autouse=True)
def _schema() -> None:
    # Start each SQLite run from a clean file so a schema change between
    # runs can't leave a stale table lying around (Postgres relies on the
    # migration + per-test cleanup instead — its DB isn't disposable here).
    url = make_url(os.environ["SOUK_DATABASE_URL"])
    if url.get_backend_name() == "sqlite" and url.database:
        for suffix in ("", "-wal", "-shm"):
            Path(url.database + suffix).unlink(missing_ok=True)
    command.upgrade(Config(str(ALEMBIC_INI)), "head")


@pytest.fixture(autouse=True)
async def _clean_db() -> AsyncIterator[None]:
    async with engine.begin() as conn:
        if _IS_POSTGRES:
            await conn.exec_driver_sql(
                "TRUNCATE providers, agents, threads, thread_history, run_events "
                "RESTART IDENTITY CASCADE"
            )
        else:
            for table in _TABLES_CHILD_FIRST:
                await conn.exec_driver_sql(f"DELETE FROM {table}")
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
