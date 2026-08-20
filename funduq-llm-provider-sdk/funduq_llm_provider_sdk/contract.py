from __future__ import annotations

CONNECTED_LLM_PROVIDER_ATTRS = frozenset({"public_key", "complete"})

DELIVERED_COMPLETION_FIELDS = frozenset(
    {"run_id", "provider_key", "agent_name", "body", "llm_name", "context", "actor_chain"}
)

KYOK_FORWARDED_PROPS_KEY = "kyok"

COMPLETION_REFUSAL_ATTR = "refusal"
