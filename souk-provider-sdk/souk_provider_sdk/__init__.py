"""What a provider and souk agree on, stated from the provider's side.

This package defines the *interaction* — identity and what it signs, the
provider port, and the provider's own worker loop. It carries no transport:
wrapping the three calls in a network is a downstream job, and the absence of
`httpx`, `grpcio` and `websockets` from this package's dependencies is what
makes that checkable rather than a matter of discipline.

An in-process provider needs nothing else — a `Souk` object satisfies
`SoukConnection` structurally, so pass one straight to `ProviderWorker`.
"""

from souk_provider_sdk.identity import (
    ProviderIdentity,
    deletion_payload,
    registration_payload,
)
from souk_provider_sdk.provider import AgentHandle, HandleProvider, Provider
from souk_provider_sdk.worker import ProviderWorker, SoukConnection

__all__ = [
    "AgentHandle",
    "HandleProvider",
    "Provider",
    "ProviderIdentity",
    "ProviderWorker",
    "SoukConnection",
    "deletion_payload",
    "registration_payload",
]
