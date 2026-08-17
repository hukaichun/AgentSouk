"""What a provider and souk agree on, stated from the provider's side.

This package defines the *interaction* — identity and what it signs, the
provider port, and the provider's own worker loop. It carries no transport:
wrapping the calls in a network is a downstream job, and the absence of
`httpx`, `grpcio` and `websockets` from this package's dependencies is what
makes that checkable rather than a matter of discipline.

It also names nothing of souk's. Runs arrive as `DeliveredRun`, this
package's own type, and results leave through two callables the caller
supplies — so souk's model fields and method names are the integrator's
business, not this package's. `contract.py` states the shapes; the one
irreducible agreement is the signing payload in `identity.py`, which is a
wire format both sides implement rather than a dependency either way.

Work is handed over rather than asked for: whoever serves this provider
offers each run, and `ProviderRuntime.deliver` is where that lands.
"""

from souk_provider_sdk.connection import SoukConnection
from souk_provider_sdk.contract import (
    CONNECTED_PROVIDER_ATTRS,
    DELIVERED_RUN_FIELDS,
    REGISTRATION_FIELDS,
    REPORT_CALLBACKS,
)
from souk_provider_sdk.inprocess import InProcessProvider
from souk_provider_sdk.identity import (
    ProviderIdentity,
    deletion_payload,
    registration_payload,
)
from souk_provider_sdk.provider import AgentHandle, DeliveredRun, HandleProvider, Provider
from souk_provider_sdk.runtime import ProviderRuntime

__all__ = [
    "CONNECTED_PROVIDER_ATTRS",
    "InProcessProvider",
    "SoukConnection",
    "DELIVERED_RUN_FIELDS",
    "REGISTRATION_FIELDS",
    "REPORT_CALLBACKS",
    "AgentHandle",
    "DeliveredRun",
    "HandleProvider",
    "Provider",
    "ProviderIdentity",
    "ProviderRuntime",
    "deletion_payload",
    "registration_payload",
]
