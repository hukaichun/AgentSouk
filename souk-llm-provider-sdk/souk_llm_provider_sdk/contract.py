"""What this package requires of whoever wires it up, written down so they
can check it — the same device, for the same reason, as
souk-provider-sdk's contract module: two codebases that must not import
each other each state the shapes, and a test that compares the statements
is what keeps them from silently disagreeing.

The completion wire shapes themselves are deliberately NOT restated here:
they come from the `openai` package on both sides, which is already one
source of truth.
"""

from __future__ import annotations

# What souk's relay needs from an LLM provider: who it is, and how to hand
# it a completion. souk's `ConnectedLLMProvider` protocol is the same two.
CONNECTED_LLM_PROVIDER_ATTRS = frozenset({"public_key", "complete"})

# Every field of a completion as this package receives one. The adapter
# (InProcessLLMProvider.complete, or a gateway's socket frame) fills these
# from whatever its own side calls them.
DELIVERED_COMPLETION_FIELDS = frozenset(
    {"run_id", "provider_key", "agent_name", "body", "llm_name", "context", "actor_chain"}
)

# Where a KYOK-enabled run's token is delivered to the *agent* provider:
# forwardedProps, under this key. Stated here because the LLM provider's
# integrator is usually also the one wiring the agent side of a test loop.
KYOK_FORWARDED_PROPS_KEY = "kyok"
