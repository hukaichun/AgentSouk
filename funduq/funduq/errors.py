from __future__ import annotations

from funduq.repo import (
    ProviderFingerprintTaken,
    ThreadNotFound,
    ThreadOwnershipMismatch,
    ThreadQueueFull,
)

__all__ = [
    "AgentInUse",
    "AgentNotFound",
    "InvalidRegistration",
    "KyokRejected",
    "InvalidRunInput",
    "ProviderFingerprintTaken",
    "RunNotFound",
    "FunduqError",
    "ThreadNotFound",
    "ThreadOwnershipMismatch",
    "ThreadQueueFull",
]


class FunduqError(Exception):
    pass


class AgentNotFound(FunduqError):
    pass


class LlmProviderNotFound(FunduqError):
    pass


class LlmOfferingInUse(FunduqError):
    """Raised when deleting an LLM offering is refused because it's still in use — the mirror of `AgentInUse`.

    `reason` is a machine-readable code distinct from the human-readable
    message: "connected" (a provider is currently serving it) or
    "active_run" (a live run is bound to it).
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class AgentInUse(FunduqError):
    """Raised when deleting an agent is refused because it's still in use.

    `reason` is a machine-readable code distinct from the human-readable
    message: "connected" (a provider is currently attached to it),
    "active_run" (it has a run that hasn't reached a terminal status), or
    "has_history" (it has prior conversation history) even after the
    provider that served it has since detached.
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class InvalidRegistration(FunduqError):
    pass


class KyokRejected(FunduqError):
    """A KYOK completion call was refused; `status` is the status code a caller should be told.

    Mapping it onto a transport is the serving layer's job. The reasons
    differ in kind, so `status` varies with them: an unusable bearer
    token or call signature is 401, a request body that isn't valid
    JSON is 400, a run that isn't currently active or an unregistered
    agent is 403, a run whose KYOK binding is gone or a detached LLM
    provider is 503, and the provider's own completion call failing is
    502.

    `refusal` carries the LLM provider's structured refusal payload when
    it raised one (else None); funduq relays it without interpreting it.
    """

    def __init__(self, message: str, *, status: int, refusal: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.refusal = refusal


class RunNotFound(FunduqError):
    pass


class InvalidRunInput(FunduqError):
    pass
