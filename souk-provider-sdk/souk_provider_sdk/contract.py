from __future__ import annotations

DELIVERED_RUN_FIELDS = frozenset(
    {"run_id", "agent_name", "run_input", "thread_id", "metadata"}
)

LINK_REPORT_METHODS = {
    "report_event": ("run_id", "event"),
    "finish_run": ("run_id",),
}

LINK_QUERY_METHODS = {
    "thread_messages": ("thread_id", "limit"),
}

CONNECTED_PROVIDER_ATTRS = frozenset(
    {"public_key", "max_concurrent_runs", "deliver", "cancel"}
)


REGISTRATION_FIELDS = frozenset({"name", "description", "agent_card_extra", "metadata"})
