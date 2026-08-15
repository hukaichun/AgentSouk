"""Liveness and readiness, as two endpoints rather than one.

They answer different questions and a single endpoint gets one of them
wrong. *Live* means this process is running and its event loop is turning —
if that is false the only useful response is a restart. *Ready* means it can
serve: the database answers and is at the migration this code was built
against. A database blip must not restart every replica, which is exactly
what happens when a liveness probe touches the database.

Both are unauthenticated, because a probe cannot hold a credential — so
neither says anything a stranger should not see. `Souk.health` is careful
about that (no connection string, no driver message); this file adds no
detail of its own.

What "ready" means is core's judgement, not this layer's — see Health.ready.
All that happens here is a status code.
"""

from fastapi import APIRouter, Depends, Response

from souk.core import Souk
from souk_server.deps import get_souk
from souk_server.models import HealthResponse, LivenessResponse

router = APIRouter()


@router.get("/healthz")
async def liveness() -> LivenessResponse:
    """Answering at all is the whole check. Deliberately touches nothing —
    no database, no background task — so a dependency being down can never
    be mistaken for this process being wedged."""
    return LivenessResponse(status="alive")


@router.get("/readyz")
async def readiness(response: Response, souk: Souk = Depends(get_souk)) -> HealthResponse:
    """503 when souk cannot serve, so a load balancer stops sending traffic
    rather than sending it into failures. The body says why in every case,
    including the healthy one — a probe that only prints "unhealthy" makes
    somebody go and find out.
    """
    health = await souk.health()
    if not health.ready:
        response.status_code = 503
    return HealthResponse(
        ready=health.ready,
        database=health.database,
        database_error=health.database_error,
        schema_revision=health.schema_revision,
        expected_schema_revision=health.expected_schema_revision,
        background_running=health.background_running,
    )
