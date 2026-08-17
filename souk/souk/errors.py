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

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class InvalidRegistration(SoukError):
    pass


class KyokRejected(SoukError):

    def __init__(self, message: str, *, status: int) -> None:
        super().__init__(message)
        self.status = status


class RunNotFound(SoukError):
    pass


class InvalidRunInput(SoukError):
    pass
