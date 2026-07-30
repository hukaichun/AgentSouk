import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from souk import api_a2a, api_agui, api_registry
from souk.config import settings
from souk.db import bootstrap_schema
from souk.grpc_server import create_grpc_server

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bootstrap_schema()
    yield


app = FastAPI(title="souk", lifespan=lifespan)
app.include_router(api_registry.router)
app.include_router(api_agui.router)
app.include_router(api_a2a.router)


async def _serve() -> None:
    await bootstrap_schema()

    grpc_server = create_grpc_server()
    await grpc_server.start()

    config = uvicorn.Config(app, host=settings.http_host, port=settings.http_port, log_level="info")
    http_server = uvicorn.Server(config)

    try:
        await asyncio.gather(http_server.serve(), grpc_server.wait_for_termination())
    finally:
        await grpc_server.stop(grace=5)


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
