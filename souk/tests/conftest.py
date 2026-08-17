
from __future__ import annotations

import os
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from souk.config import CoreSettings
from souk.core import Souk
from souk_provider_sdk import InProcessLink, ProviderIdentity, ProviderRuntime

ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"

TEST_SIGNING_SECRET = "test-signing-secret"

DATABASE_URL = os.environ.get(
    "SOUK_DATABASE_URL", f"sqlite+aiosqlite:///{Path(tempfile.gettempdir()) / 'souk_pytest.db'}"
)

_TABLES_CHILD_FIRST = (
    "run_events",
    "thread_messages",
    "runs",
    "threads",
    "agents",
    "llm_providers",
    "providers",
)


@pytest.fixture(scope="session")
def settings() -> CoreSettings:
    return CoreSettings(database_url=DATABASE_URL, token_signing_secret=TEST_SIGNING_SECRET)


@pytest.fixture(scope="session")
def souk(settings: CoreSettings) -> Souk:
    return Souk(settings)


@pytest.fixture(scope="session", autouse=True)
def _schema(settings: CoreSettings) -> None:
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and url.database:
        for suffix in ("", "-wal", "-shm"):
            Path(url.database + suffix).unlink(missing_ok=True)
    os.environ["SOUK_DATABASE_URL"] = settings.database_url
    command.upgrade(Config(str(ALEMBIC_INI)), "head")


@pytest.fixture(autouse=True)
async def _dispatching(souk: Souk) -> AsyncIterator[None]:
    souk.broker.start()
    try:
        yield
    finally:
        souk.broker.stop()


@pytest.fixture(autouse=True)
async def _clean_db(souk: Souk) -> AsyncIterator[None]:
    is_postgres = souk.engine.sync_engine.dialect.name == "postgresql"
    async with souk.engine.begin() as conn:
        if is_postgres:
            await conn.exec_driver_sql(
                "TRUNCATE providers, agents, threads, runs, thread_messages, run_events, "
                "llm_providers RESTART IDENTITY CASCADE"
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

    def __init__(self) -> None:
        super().__init__(Ed25519PrivateKey.generate())

    def sign_chain_hop(self, subject: dict, prev_token: str | None = None, exp_offset: int = 300) -> str:
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


@pytest.fixture
async def attach(souk: Souk):
    """Attach a provider the way a real one arrives: the SDK's runtime, with
    an adapter in front of it that souk can hand a run to.

    souk used to take a bare object with `run_stream` and build the loop
    around it itself. It does not any more — the loop is the provider's — so
    a test that wants an agent served goes through the same steps a
    deployment does: make a runtime, adapt it, attach it.

    Every runtime is stopped when the test ends. The `souk` fixture is
    session-scoped, so one left running stays registered with the broker and
    takes the next test's runs.
    """
    started: list[ProviderRuntime] = []

    async def _attach(identity: ProviderIdentity, provider, names, **kwargs) -> ProviderRuntime:
        runtime = ProviderRuntime(identity, provider, **kwargs)
        started.append(runtime)
        runtime.start()
        await souk.attach_provider(InProcessLink(souk, runtime), list(names))
        return runtime

    yield _attach
    for runtime in started:
        await runtime.aclose(cancel_in_flight=True)


class EchoAgent:
    """A provider that answers with one short message and remembers who
    called it. Shared because two suites need exactly this and a test module
    importing another test module is how deleting one file broke a second."""

    def __init__(self) -> None:
        self.seen_caller: dict | None = None

    async def run_stream(self, agent_name: str, run_input: dict):
        self.seen_caller = (run_input.get("forwardedProps") or {}).get("caller")
        ids = {"threadId": run_input["threadId"], "runId": run_input["runId"]}
        yield {"type": "RUN_STARTED", **ids}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "done"}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", **ids}


@dataclass
class Served:
    """What `serve` hands back: everything a test needs to talk about the
    provider it just stood up."""

    identity: Identity
    provider: Any
    runtime: ProviderRuntime
    agents: dict


@pytest.fixture
async def serve(souk: Souk, attach):
    """Register a provider's agents and attach it, in one step.

    Both halves, because they are always done together and neither is
    optional: registration is what makes the names souk's to serve, and
    attaching is what makes them reachable.
    """

    async def _serve(provider=None, *names: str, **kwargs) -> Served:
        provider = EchoAgent() if provider is None else provider
        names = names or ("agent",)
        identity = Identity()
        signature, timestamp = identity.sign_registration(list(names))
        registration = await souk.register_agents(
            identity.public_key, signature, timestamp, [{"name": n} for n in names]
        )
        runtime = await attach(identity, provider, names, **kwargs)
        return Served(identity, provider, runtime, registration.agents)

    return _serve


@pytest.fixture
async def register(souk: Souk):
    """Register agents without attaching anything — for the cases that are
    about souk knowing a name, not about anybody serving it."""

    async def _register(*names: str) -> Served:
        identity = Identity()
        signature, timestamp = identity.sign_registration(list(names))
        registration = await souk.register_agents(
            identity.public_key, signature, timestamp, [{"name": n} for n in names]
        )
        return Served(identity, None, None, registration.agents)

    return _register
