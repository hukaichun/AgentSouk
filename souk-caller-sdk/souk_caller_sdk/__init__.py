"""What a caller and souk agree on, stated from the caller's side.

The peer of `souk_provider_sdk`, one relationship over. That package is what a
provider and souk agree on; this is the other edge — specifically the Keep Your
Own Key edge, where a provider's LLM call comes back to whoever is paying for
it and is answered with their own key.

Two absences are the design:

- **No transport.** No `httpx`, no `websockets`. Wrapping this in a network is
  a downstream job, and an empty dependency list is what makes that checkable
  rather than a matter of discipline. `InProcessLink` is a carrier like any
  other, and it is what lets the whole loop be a test in souk's own suite
  instead of a gateway, three processes and a real key.
- **No LLM client.** No `litellm`, no `openai`. You supply a
  `CompletionSource`; which model, which vendor, which key and what it costs
  are yours. A package that chose for you would be making the single decision
  KYOK exists to leave you.

It also names nothing of souk's. Completions arrive as `PendingCompletion` and
answers leave through a `CallerLink`; `contract.py` states the shapes and
`inprocess.py` is the one module that knows both sides' words.

Starting runs, reading threads and browsing the roster are the caller's *other*
relationship with souk, over its public AG-UI/A2A surface. They are not here,
and a method that only exists to mirror souk's API does not belong here either.
"""

from souk_caller_sdk.bridge import (
    CLAIM_WAIT_SECONDS,
    CompletionSource,
    KyokBridge,
    new_session_id,
    run_metadata,
)
from souk_caller_sdk.contract import (
    COMPLETION_SOURCE_ARGS,
    ERROR_CHUNK_KEY,
    LINK_METHODS,
    PENDING_COMPLETION_FIELDS,
    RUN_METADATA_FIELDS,
    RUN_METADATA_KEY,
)
from souk_caller_sdk.inprocess import InProcessLink
from souk_caller_sdk.link import CallerLink, PendingCompletion

__all__ = [
    "CLAIM_WAIT_SECONDS",
    "COMPLETION_SOURCE_ARGS",
    "CallerLink",
    "CompletionSource",
    "ERROR_CHUNK_KEY",
    "InProcessLink",
    "KyokBridge",
    "LINK_METHODS",
    "PENDING_COMPLETION_FIELDS",
    "PendingCompletion",
    "RUN_METADATA_FIELDS",
    "RUN_METADATA_KEY",
    "new_session_id",
    "run_metadata",
]
