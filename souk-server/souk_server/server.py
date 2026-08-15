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

from souk_server import api_a2a, api_agui, api_health, api_llm_bridge, api_registry
from souk.config import CoreSettings
from souk_server.config import ServingSettings
from souk.core import Souk
from souk_server.deps import install_error_handlers
from souk_server.grpc_server import create_grpc_server

logger = logging.getLogger("souk_server")
logging.basicConfig(level=logging.INFO)


def create_app(souk: Souk, serving: ServingSettings | None = None) -> FastAPI:
    """Builds the ASGI app for `souk`. Does not bind anything — the caller
    decides how (or whether) it reaches a network, and is free to wrap it in
    their own middleware or mount it inside a larger app.
    """
    serving = serving or ServingSettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Bringing souk up is souk's own business — reconciling what the last
        # process left behind and keeping the health sweeps running (see
        # Souk.start). This layer only decides *when*, and may call it after
        # _serve already has: start() runs once.
        #
        # The schema is not part of it: `alembic upgrade head` (see
        # souk/alembic/) is a deploy-time step with DDL-capable credentials,
        # separate from starting the server, which only ever runs DML against
        # a possibly DML-only role.
        await souk.start()
        yield
        # Deliberately no aclose: this app was handed a Souk it does not own
        # (see create_app's docstring — it may be mounted inside a larger
        # app), and closing someone else's would take their background work
        # and their connection pool with it. Whoever constructed it closes
        # it; _serve below does exactly that for the one it constructs.

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
    install_error_handlers(app)
    app.include_router(api_health.router)
    app.include_router(api_registry.router)
    app.include_router(api_agui.router)
    app.include_router(api_a2a.router)
    app.include_router(api_llm_bridge.router)
    return app


async def _serve() -> None:
    souk = Souk(CoreSettings())
    serving = ServingSettings()
    app = create_app(souk, serving)

    # Ahead of the gRPC server: it must not accept PollForWork/AgentSession
    # traffic before reconciliation has run. uvicorn's Server.serve() below
    # triggers the app's ASGI lifespan, which calls this again — and that is
    # simply a no-op now, rather than a second reconciliation pass justified
    # by the window between the two usually being empty.
    await souk.start()

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
