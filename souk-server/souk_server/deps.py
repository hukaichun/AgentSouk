"""FastAPI dependencies that resolve the running `Souk` instance.

Replaces `souk.db.get_session`, which could only ever hand out sessions from
one import-time global engine. The Souk is put on the app (see
souk_server.server.create_app) and read back off the request here, so the HTTP layer
holds no module-level state of its own and two apps in one process can serve
two differently-configured souks.

This module is part of the serving layer, not core — it imports FastAPI. It
moves to the souk-server subproject when the packages split; see
docs/library-architecture.md.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from souk_server.config import ServingSettings
from souk.core import Souk
from souk.errors import (
    AgentNotFound,
    AmbiguousAgentName,
    InvalidRegistration,
    ProviderFingerprintTaken,
    InvalidRunInput,
    KyokRejected,
    RunNotFound,
    SoukError,
    ThreadNotFound,
    ThreadOwnershipMismatch,
)
from souk.identity import InvalidActorChain


def get_souk(request: Request) -> Souk:
    return request.app.state.souk


def get_serving_settings(request: Request) -> ServingSettings:
    return request.app.state.serving_settings


async def get_session(souk: Souk = Depends(get_souk)) -> AsyncIterator[AsyncSession]:
    async with souk.session() as session:
        yield session


# souk.errors -> HTTP status. One mapping, registered once, rather than a
# try/except in every route: which status a given failure deserves is a
# property of the failure, not of the endpoint that happened to hit it. Both
# protocol surfaces raise the same errors, so writing this per-route meant
# writing it twice and letting the two drift.
_STATUS = {
    AgentNotFound: 404,
    ThreadNotFound: 404,
    RunNotFound: 404,
    AmbiguousAgentName: 409,
    ThreadOwnershipMismatch: 409,
    ProviderFingerprintTaken: 409,
    InvalidRegistration: 401,
    InvalidActorChain: 401,
    InvalidRunInput: 400,
}


def _detail(exc: Exception) -> object:
    """A display name is not exclusive across identities, so several matches
    is a normal outcome the caller has to resolve — answered with the
    candidates and the unambiguous route to retry against, rather than a bare
    message it can do nothing with."""
    if isinstance(exc, AmbiguousAgentName):
        return {
            "error": f"multiple agents are registered under the name '{exc.name}'",
            "retry_with": "/agui/id/{agent_id} or /a2a/id/{agent_id}/...",
            "candidates": [
                {
                    "name": c["name"],
                    "agent_id": c["agent_id"],
                    "public_key_prefix": c["public_key"][:12],
                    "joined_at": c["joined_at"].isoformat(),
                    "description": c["agent_card"].get("description", ""),
                }
                for c in exc.candidates
            ],
        }
    if isinstance(exc, ThreadNotFound):
        return f"thread '{exc}' not found"
    if isinstance(exc, InvalidActorChain):
        return f"invalid actor chain: {exc}"
    return str(exc)


def install_error_handlers(app: FastAPI) -> None:
    """Translate souk's domain errors into responses for this app.

    Serving's job, not core's: an adapter says "no such agent" without
    knowing whether anyone is listening over HTTP. Any host mounting souk's
    routers needs this (or its own equivalent), or a domain error surfaces
    as a 500.
    """

    async def handle(_request: Request, exc: Exception) -> JSONResponse:
        status = _STATUS.get(type(exc), 500)
        if isinstance(exc, KyokRejected):
            status = exc.status
        return JSONResponse(status_code=status, content={"detail": _detail(exc)})

    for error_type in (*_STATUS, KyokRejected, SoukError):
        app.add_exception_handler(error_type, handle)
