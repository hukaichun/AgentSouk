from __future__ import annotations

import os
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from funduq.config import CoreSettings
from funduq.core import Funduq
from funduq.migrate import migrate as funduq_migrate
from funduq_provider_sdk import InProcessLink, ProviderIdentity, ProviderRuntime


TEST_SIGNING_SECRET = "test-signing-secret"

DATABASE_URL = os.environ.get(
    "FUNDUQ_DATABASE_URL", f"sqlite+aiosqlite:///{Path(tempfile.gettempdir()) / 'funduq_pytest.db'}"
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
def funduq(settings: CoreSettings) -> Funduq:
    return Funduq(settings)


@pytest.fixture(scope="session", autouse=True)
def _schema(settings: CoreSettings) -> None:
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite" and url.database:
        for suffix in ("", "-wal", "-shm"):
            Path(url.database + suffix).unlink(missing_ok=True)
    os.environ["FUNDUQ_DATABASE_URL"] = settings.database_url
    funduq_migrate(settings.database_url)


@pytest.fixture(autouse=True)
async def _dispatching(funduq: Funduq) -> AsyncIterator[None]:
    funduq.broker.start()
    try:
        yield
    finally:
        funduq.broker.stop()


@pytest.fixture(autouse=True)
async def _clean_db(funduq: Funduq) -> AsyncIterator[None]:
    is_postgres = funduq.engine.sync_engine.dialect.name == "postgresql"
    async with funduq.engine.begin() as conn:
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
async def session(funduq: Funduq) -> AsyncIterator[AsyncSession]:
    async with funduq.session() as s:
        yield s


class Identity(ProviderIdentity):

    def __init__(self) -> None:
        super().__init__(Ed25519PrivateKey.generate())

    def sign_chain_hop(self, prev_token: str | None = None, exp_offset: int = 300) -> str:
        return self.sign_hop(prev_token, ttl=exp_offset)

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
async def attach(funduq: Funduq):
    started: list[ProviderRuntime] = []

    async def _attach(identity: ProviderIdentity, provider, names, **kwargs) -> ProviderRuntime:
        runtime = ProviderRuntime(identity, provider, **kwargs)
        started.append(runtime)
        runtime.start()
        await funduq.attach_provider(InProcessLink(funduq, runtime), list(names))
        return runtime

    yield _attach
    for runtime in started:
        await runtime.aclose(cancel_in_flight=True)


class EchoAgent:

    def __init__(self) -> None:
        self.seen_chain: list | None = None

    async def run_stream(self, agent_name: str, run_input):
        self.seen_chain = (run_input.forwarded_props or {}).get("actorChain")
        ids = {"threadId": run_input.thread_id, "runId": run_input.run_id}
        yield {"type": "RUN_STARTED", **ids}
        yield {"type": "TEXT_MESSAGE_START", "messageId": "m1", "role": "assistant"}
        yield {"type": "TEXT_MESSAGE_CONTENT", "messageId": "m1", "delta": "done"}
        yield {"type": "TEXT_MESSAGE_END", "messageId": "m1"}
        yield {"type": "RUN_FINISHED", **ids}


@dataclass
class Served:

    identity: Identity
    provider: Any
    runtime: ProviderRuntime
    agents: dict


@pytest.fixture
async def serve(funduq: Funduq, attach):

    async def _serve(provider=None, *names: str, **kwargs) -> Served:
        provider = EchoAgent() if provider is None else provider
        names = names or ("agent",)
        identity = Identity()
        signature, timestamp = identity.sign_registration(list(names))
        registration = await funduq.register_agents(
            identity.public_key, signature, timestamp, [{"name": n} for n in names]
        )
        runtime = await attach(identity, provider, names, **kwargs)
        return Served(identity, provider, runtime, registration.agents)

    return _serve


@pytest.fixture
async def register(funduq: Funduq):

    async def _register(*names: str) -> Served:
        identity = Identity()
        signature, timestamp = identity.sign_registration(list(names))
        registration = await funduq.register_agents(
            identity.public_key, signature, timestamp, [{"name": n} for n in names]
        )
        return Served(identity, None, None, registration.agents)

    return _register
