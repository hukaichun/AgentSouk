"""`/healthz` and `/readyz`, and why they are two endpoints.

The distinction is the whole point: a liveness probe that touches the
database restarts every replica during a database blip, turning a recoverable
outage into a stampede. So `/healthz` answers from nothing at all, and
`/readyz` is the one allowed to fail.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from souk.config import CoreSettings
from souk.core import Souk
from souk.db_schema import EXPECTED_SCHEMA_REVISION
from souk_server.server import create_app


async def test_healthz_answers_from_nothing(settings: CoreSettings) -> None:
    """Pointed at a database that cannot be reached, so that a passing
    liveness check proves it consulted nothing — the process is up, which is
    the only thing this endpoint is for."""
    souk = Souk(
        settings.model_copy(
            update={"database_url": "postgresql+psycopg://nobody:hunter2@127.0.0.1:1/none"}
        )
    )
    try:
        transport = ASGITransport(app=create_app(souk))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/healthz")

        assert resp.status_code == 200
        assert resp.json() == {"status": "alive"}
    finally:
        await souk.aclose()


async def test_readyz_is_200_and_says_why(client) -> None:
    resp = await client.get("/readyz")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ready"] is True
    assert body["database"] is True
    assert body["schema_revision"] == EXPECTED_SCHEMA_REVISION == body["expected_schema_revision"]


async def test_readyz_is_503_when_the_database_is_gone_and_leaks_nothing(
    settings: CoreSettings,
) -> None:
    """503 so a load balancer takes this replica out rather than sending
    traffic into failures. The body names the failure without naming the
    database — this endpoint is unauthenticated, since a probe cannot hold a
    credential.
    """
    souk = Souk(
        settings.model_copy(
            update={"database_url": "postgresql+psycopg://nobody:hunter2@127.0.0.1:1/none"}
        )
    )
    try:
        transport = ASGITransport(app=create_app(souk))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/readyz")

        assert resp.status_code == 503
        body = resp.json()
        assert body["ready"] is False
        assert body["database"] is False
        assert body["database_error"] == "OperationalError"
        assert not any(secret in resp.text for secret in ("nobody", "hunter2", "127.0.0.1"))
    finally:
        await souk.aclose()
