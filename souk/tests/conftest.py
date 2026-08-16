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
from souk_provider_sdk import ProviderIdentity

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

TEST_SIGNING_SECRET = "test-signing-secret"

# Postgres when a DSN is exported, a throwaway SQLite file otherwise.
DATABASE_URL = os.environ.get(
    "SOUK_DATABASE_URL", f"sqlite+aiosqlite:///{Path(tempfile.gettempdir()) / 'souk_pytest.db'}"
)

# FK-safe teardown order (children before parents) for the SQLite path,
# where there's no TRUNCATE ... CASCADE. Postgres uses TRUNCATE directly.
_TABLES_CHILD_FIRST = ("run_events", "thread_messages", "runs", "threads", "agents", "providers")


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
                "TRUNCATE providers, agents, threads, runs, thread_messages, run_events "
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


class Identity(ProviderIdentity):
    """A throwaway provider, signing the way a real one does.

    This is `souk_provider_sdk.ProviderIdentity`, deliberately, and not a
    local reimplementation: the SDK states the payload bytes independently of
    souk, so every test that registers is checking that souk still accepts
    what a provider actually sends.

    The version this replaced said in its own docstring that it mirrored the
    SDK's signing "exactly" and then called `souk.identity`'s builder. The two
    could not disagree, so when souk's payload gained an operation prefix,
    both sides moved together: 219 tests passed and no provider could
    register. A copy that is really a call is not a second opinion.
    """

    def __init__(self) -> None:
        super().__init__(Ed25519PrivateKey.generate())

    def sign_chain_hop(self, subject: dict, prev_token: str | None = None, exp_offset: int = 300) -> str:
        """`exp_offset` may be negative, to build an already-expired hop for
        souk.identity.verify_actor_chain's per-hop expiry handling."""
        return self.sign_hop(subject, prev_token, ttl=exp_offset)

    def register_body(self, agents: list[dict]) -> dict:
        signature, timestamp = self.sign_registration([a["name"] for a in agents])
        return {
            "public_key": self.public_key,
            "signature": signature,
            "timestamp": timestamp,
            "agents": agents,
        }


@pytest.fixture
def new_identity() -> type[Identity]:
    return Identity
