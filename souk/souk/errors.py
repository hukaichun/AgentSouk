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
    pass


class AgentNotFound(SoukError):
    pass


class LlmProviderNotFound(SoukError):
    pass


class AgentInUse(SoukError):
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


class InvalidRegistration(SoukError):
    pass


class KyokRejected(SoukError):
    """A KYOK completion call was refused; `status` is the HTTP status the caller should see.

    The reasons differ in kind, so `status` varies with them: an
    unusable bearer token or call signature is 401, a run that is no
    longer bound or an unregistered agent is 403, a detached LLM
    provider is 503, and the provider's own completion call failing is
    502.
    """

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class RunNotFound(SoukError):
    pass


class InvalidRunInput(SoukError):
    pass
