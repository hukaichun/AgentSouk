"""FastAPI dependencies that resolve the running `Souk` instance.

Replaces `souk.db.get_session`, which could only ever hand out sessions from
one import-time global engine. The Souk is put on the app (see
souk.server.create_app) and read back off the request here, so the HTTP layer
holds no module-level state of its own and two apps in one process can serve
two differently-configured souks.

This module is part of the serving layer, not core — it imports FastAPI. It
moves to the souk-server subproject when the packages split; see
docs/library-architecture.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from souk.config import ServingSettings
from souk.core import Souk


def get_souk(request: Request) -> Souk:
    return request.app.state.souk


def get_serving_settings(request: Request) -> ServingSettings:
    return request.app.state.serving_settings


async def get_session(souk: Souk = Depends(get_souk)) -> AsyncIterator[AsyncSession]:
    async with souk.session() as session:
        yield session
