"""What a provider and souk agree on, stated from the provider's side.

This package defines the *interaction* — identity and what it signs, the
provider port, and the provider's own worker loop. It carries no transport:
wrapping the three calls in a network is a downstream job, and the absence of
`httpx`, `grpcio` and `websockets` from this package's dependencies is what
makes that checkable rather than a matter of discipline.

souk hands work over rather than being asked for it: the broker offers each
run to whoever serves its agent, and `ProviderRuntime.deliver` is where that
lands. An in-process provider passes a `Souk` straight in — it has the two
calls the runtime reports through.
"""

from souk_provider_sdk.contract import (
    AGENT_FIELDS,
    CLAIMED_RUN_FIELDS,
)
from souk_provider_sdk.identity import (
    ProviderIdentity,
    deletion_payload,
    registration_payload,
)
from souk_provider_sdk.provider import AgentHandle, HandleProvider, Provider
from souk_provider_sdk.runtime import ProviderRuntime

__all__ = [
    "AGENT_FIELDS",
    "AgentHandle",
    "CLAIMED_RUN_FIELDS",
    "HandleProvider",
    "Provider",
    "ProviderIdentity",
    "ProviderRuntime",
    "deletion_payload",
    "registration_payload",
]
