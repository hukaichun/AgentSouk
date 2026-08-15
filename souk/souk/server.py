"""The reference gateway: assembles a Souk into an HTTP + gRPC server.

This is the serving layer. It is the only place that binds a port, applies
CORS, or terminates TLS — every such decision belongs to whoever hosts souk,
not to souk itself, which is why `create_app` hands back a plain ASGI app and
`main` is a thin wrapper that happens to serve it. This module moves to the
souk-server subproject in a later step; see docs/library-architecture.md.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from souk import api_a2a, api_agui, api_llm_bridge, api_registry, repo
from souk.config import CoreSettings, ServingSettings
from souk.core import Souk
from souk.grpc_server import create_grpc_server
from souk.health import run_health_sweeps_forever

logger = logging.getLogger("souk.server")
logging.basicConfig(level=logging.INFO)


async def startup(souk: Souk) -> None:
    # Schema must already exist: `alembic upgrade head` (see souk/alembic/)
    # is a deploy-time step run with DDL-capable credentials, separate from
    # starting the server — souk itself only ever runs DML against
    # settings.database_url, which may be a DML-only role.
    async with souk.session() as session:
        orphaned = await repo.fail_orphaned_runs(session)
    if orphaned:
        logger.warning(
            "startup: marked %d run(s) failed — still queued/running from before this restart, "
            "souk's in-memory dispatch state doesn't survive a restart: %s",
            len(orphaned),
            orphaned,
        )


def create_app(souk: Souk, serving: ServingSettings | None = None) -> FastAPI:
    """Builds the ASGI app for `souk`. Does not bind anything — the caller
    decides how (or whether) it reaches a network, and is free to wrap it in
    their own middleware or mount it inside a larger app.
    """
    serving = serving or ServingSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await startup(souk)
        sweeper = souk.spawn(run_health_sweeps_forever(souk), name="health-sweeps")
        try:
            yield
        finally:
            sweeper.cancel()

    app = FastAPI(title="souk", lifespan=lifespan)
    # Read back by souk.deps' dependencies, so the routers hold no
    # module-level state and two apps can serve two different souks.
    app.state.souk = souk
    app.state.serving_settings = serving
    app.add_middleware(
        CORSMiddleware,
        allow_origins=serving.cors_allow_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_registry.router)
    app.include_router(api_agui.router)
    app.include_router(api_a2a.router)
    app.include_router(api_llm_bridge.router)
    return app


async def _serve() -> None:
    souk = Souk(CoreSettings())
    serving = ServingSettings()
    app = create_app(souk, serving)

    # Explicit call, ahead of starting the gRPC server: it must not accept
    # PollForWork/AgentSession traffic before startup's cleanup has run.
    # uvicorn's Server.serve() below also triggers the FastAPI app's ASGI
    # lifespan, which calls startup() again (harmless — fail_orphaned_runs
    # is idempotent) but is where the health-sweep background task
    # actually gets started; it isn't started here too, to avoid two
    # redundant sweep loops running concurrently.
    await startup(souk)

    grpc_server = create_grpc_server(souk, serving)
    await grpc_server.start()

    if not (serving.http_tls_cert_path and serving.http_tls_key_path):
        logger.warning(
            "HTTP server listening on %s:%s WITHOUT TLS — fine for same-host development, "
            "never for a souk reachable over a real network (see souk.config's http_tls_* settings)",
            serving.http_host,
            serving.http_port,
        )
    config = uvicorn.Config(
        app,
        host=serving.http_host,
        port=serving.http_port,
        log_level="info",
        ssl_certfile=serving.http_tls_cert_path,
        ssl_keyfile=serving.http_tls_key_path,
    )
    http_server = uvicorn.Server(config)

    try:
        await asyncio.gather(http_server.serve(), grpc_server.wait_for_termination())
    finally:
        await grpc_server.stop(grace=5)
        await souk.aclose()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
