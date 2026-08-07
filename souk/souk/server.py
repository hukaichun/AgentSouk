import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from souk import api_a2a, api_agui, api_llm_bridge, api_registry, repo
from souk.config import settings
from souk.db import SessionLocal, bootstrap_schema
from souk.grpc_server import create_grpc_server
from souk.health import run_health_sweeps_forever

logger = logging.getLogger("souk.server")
logging.basicConfig(level=logging.INFO)


async def startup() -> None:
    await bootstrap_schema()
    async with SessionLocal() as session:
        orphaned = await repo.fail_orphaned_runs(session)
    if orphaned:
        logger.warning(
            "startup: marked %d run(s) failed — still queued/running from before this restart, "
            "souk's in-memory dispatch state doesn't survive a restart: %s",
            len(orphaned),
            orphaned,
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup()
    sweeper = asyncio.create_task(run_health_sweeps_forever())
    try:
        yield
    finally:
        sweeper.cancel()


app = FastAPI(title="souk", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_registry.router)
app.include_router(api_agui.router)
app.include_router(api_a2a.router)
app.include_router(api_llm_bridge.router)


async def _serve() -> None:
    # Explicit call, ahead of starting the gRPC server: it must not accept
    # PollForWork/AgentSession traffic before the schema exists. uvicorn's
    # Server.serve() below also triggers the FastAPI app's ASGI lifespan,
    # which calls startup() again (harmless — bootstrap_schema and
    # fail_orphaned_runs are idempotent) but is where the health-sweep
    # background task actually gets started; it isn't started here too,
    # to avoid two redundant sweep loops running concurrently.
    await startup()

    grpc_server = create_grpc_server()
    await grpc_server.start()

    if not (settings.http_tls_cert_path and settings.http_tls_key_path):
        logger.warning(
            "HTTP server listening on %s:%s WITHOUT TLS — fine for same-host development, "
            "never for a souk reachable over a real network (see souk.config's http_tls_* settings)",
            settings.http_host,
            settings.http_port,
        )
    config = uvicorn.Config(
        app,
        host=settings.http_host,
        port=settings.http_port,
        log_level="info",
        ssl_certfile=settings.http_tls_cert_path,
        ssl_keyfile=settings.http_tls_key_path,
    )
    http_server = uvicorn.Server(config)

    try:
        await asyncio.gather(http_server.serve(), grpc_server.wait_for_termination())
    finally:
        await grpc_server.stop(grace=5)


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
