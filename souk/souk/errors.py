"""Errors the domain and protocol layers raise, for serving to translate.

An adapter must be able to say "no such agent" or "that thread belongs to
someone else" without knowing whether anyone is listening over HTTP, so it
raises these instead of an `HTTPException`. Mapping them onto status codes is
the serving layer's job — the only layer that knows what a status code is.

`repo` keeps its own ThreadNotFound / ThreadOwnershipMismatch, which predate
this module and are equally part of the vocabulary; they are re-exported here
so a caller has one place to import from.
"""

from __future__ import annotations

from souk.repo import ThreadNotFound, ThreadOwnershipMismatch

__all__ = [
    "AgentNotFound",
    "AmbiguousAgentName",
    "InvalidRunInput",
    "RunNotFound",
    "SoukError",
    "ThreadNotFound",
    "ThreadOwnershipMismatch",
]


class SoukError(Exception):
    """Base for everything below, so serving can catch the family."""


class AgentNotFound(SoukError):
    """No agent under this id or name — or it has been de-listed, which
    callers see as the same thing."""


class AmbiguousAgentName(SoukError):
    """A display name resolved to more than one agent. Names are not
    exclusive across identities (see repo.register_agents), so this is a
    normal outcome the caller has to disambiguate, not a failure.
    `candidates` carries what it needs to choose.
    """

    def __init__(self, name: str, candidates: list[dict]) -> None:
        super().__init__(f"agent name '{name}' matches {len(candidates)} agents")
        self.name = name
        self.candidates = candidates


class RunNotFound(SoukError):
    """No such run, or not one belonging to the agent being addressed."""


class InvalidRunInput(SoukError):
    """The caller's run input didn't validate against AG-UI's schema."""
