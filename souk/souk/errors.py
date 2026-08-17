"""Errors the domain and protocol layers raise, for serving to translate.

An adapter must be able to say "no such agent" or "that thread belongs to
someone else" without knowing whether anyone is listening over HTTP, so it
raises these instead of an `HTTPException`. Mapping them onto status codes is
the serving layer's job — the only layer that knows what a status code is.

`repo` keeps its own ThreadNotFound / ThreadOwnershipMismatch /
ProviderFingerprintTaken — the first two predate this module and the third is
raised by a database constraint rather than by a decision, which is where it
belongs. All are equally part of the vocabulary and are re-exported here so a
caller has one place to import from.
"""

from __future__ import annotations

from souk.repo import ProviderFingerprintTaken, ThreadNotFound, ThreadOwnershipMismatch

__all__ = [
    "AgentInUse",
    "AgentNotFound",
    "InvalidRegistration",
    "KyokRejected",
    "InvalidRunInput",
    "ProviderFingerprintTaken",
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


class LlmProviderNotFound(SoukError):
    """No LLM offering under this (provider_key, name) pair — the KYOK
    mirror of AgentNotFound, raised for the same acts: attaching for an
    offering the key never registered, or binding a run to one that does
    not exist."""


class AgentInUse(SoukError):
    """Refused a deletion because something is still using the agent.

    Deleting is for a registration that never became anything — a typo in a
    name, a test, a batch pushed from the wrong config. Retiring a working
    agent is a different act and needs no deletion at all: stop offering it,
    and it goes offline and eventually off the roster with its record and
    everything it did intact (see `Souk.register_agents`).

    So an agent that has run even once can never be removed, only silenced.
    That is the accepted consequence of the rule the foreign key already
    states: a thread must name an agent, therefore an agent with threads
    cannot go.

    `reason` says which of the four checks refused, because "in use" is four
    different situations to an operator: still checking in, still attached in
    this process, mid-run, or has a conversation behind it.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class InvalidRegistration(SoukError):
    """A registration didn't prove it holds the key it claims — a bad
    signature, or a timestamp too far from souk's clock to rule out a
    replay. Applies identically to a provider in this process and one
    across a network: being in-process is not a reason to be trusted."""


class KyokRejected(SoukError):
    """A KYOK completion was refused. Carries the status a caller should be
    told, because the reasons differ in kind — an unusable token (401), a run
    that is no longer live or an agent that isn't registered (403), and nobody
    claiming the completion in time (502) — and flattening them would lose
    information the caller needs to know whether to retry.
    """

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class RunNotFound(SoukError):
    """No such run, or not one belonging to the agent being addressed."""


class InvalidRunInput(SoukError):
    """The caller's run input didn't validate against AG-UI's schema."""
