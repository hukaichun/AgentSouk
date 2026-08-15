"""Test fixtures for souk's test suite.

Runs against SQLite by default — zero configuration, no database to stand
up first. The same suite runs against Postgres by exporting SOUK_DATABASE_URL
(a `postgresql+psycopg://…` DSN) before invoking pytest; souk's schema and
queries are dialect-neutral (see souk/schema.py, souk/repo.py), so both
backends exercise the same semantics. See CONTRIBUTING.md for the Postgres
setup.

Settings are constructed explicitly here (see souk/core.py) rather than
arranged in `os.environ` before the first souk import — that ordering
constraint is exactly what injecting settings removed.

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

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from souk.config import CoreSettings
from souk.core import Souk
from souk.identity import registration_signing_payload

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

TEST_SIGNING_SECRET = "test-signing-secret"

# Postgres when a DSN is exported, a throwaway SQLite file otherwise.
DATABASE_URL = os.environ.get(
    "SOUK_DATABASE_URL", f"sqlite+aiosqlite:///{Path(tempfile.gettempdir()) / 'souk_pytest.db'}"
)

# FK-safe teardown order (children before parents) for the SQLite path,
# where there's no TRUNCATE ... CASCADE. Postgres uses TRUNCATE directly.
_TABLES_CHILD_FIRST = ("run_events", "thread_history", "threads", "agents", "providers")


@pytest.fixture(scope="session")
def settings() -> CoreSettings:
    return CoreSettings(database_url=DATABASE_URL, token_signing_secret=TEST_SIGNING_SECRET)


@pytest.fixture(scope="session")
def souk(settings: CoreSettings) -> Souk:
    return Souk(settings)


@pytest.fixture(scope="session", autouse=True)
def _schema(settings: CoreSettings) -> None:
    # Start each SQLite run from a clean file so a schema change between
    # runs can't leave a stale table lying around (Postgres relies on the
    # migration + per-test cleanup instead — its DB isn't disposable here).
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and url.database:
        for suffix in ("", "-wal", "-shm"):
            Path(url.database + suffix).unlink(missing_ok=True)
    # alembic/env.py reads SOUK_DATABASE_URL from the environment (it
    # deliberately doesn't import souk.config), so migrations still go
    # through the environment even though the app itself no longer does.
    os.environ["SOUK_DATABASE_URL"] = settings.database_url
    command.upgrade(Config(str(ALEMBIC_INI)), "head")


@pytest.fixture(autouse=True)
async def _clean_db(souk: Souk) -> AsyncIterator[None]:
    is_postgres = souk.engine.sync_engine.dialect.name == "postgresql"
    async with souk.engine.begin() as conn:
        if is_postgres:
            await conn.exec_driver_sql(
                "TRUNCATE providers, agents, threads, thread_history, run_events "
                "RESTART IDENTITY CASCADE"
            )
        else:
            for table in _TABLES_CHILD_FIRST:
                await conn.exec_driver_sql(f"DELETE FROM {table}")
    yield


@pytest.fixture
async def session(souk: Souk) -> AsyncIterator[AsyncSession]:
    async with souk.session() as s:
        yield s


class Identity:
    """A throwaway Ed25519 keypair plus a helper to build a signed
    registration — mirrors souk_agent_sdk.identity's
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

    def register_body(self, agents: list[dict]) -> dict:
        timestamp = int(time.time())
        payload = registration_signing_payload([a["name"] for a in agents], timestamp)
        return {
            "public_key": self.public_key,
            "signature": self._key.sign(payload).hex(),
            "timestamp": timestamp,
            "agents": agents,
        }


@pytest.fixture
def new_identity() -> type[Identity]:
    return Identity
