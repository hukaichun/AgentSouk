"""Souk-assigned entity ids: `<category>_<hex>`, no dashes.

Ids for souk's own entities (threads, runs, A2A tasks) are always assigned
by souk itself, never accepted from callers — callers only ever echo back
an id souk previously gave them (e.g. a thread_id to continue a
conversation). This keeps id formatting/uniqueness centralized instead of
trusting client-supplied values.
"""

import secrets


def new_id(category: str) -> str:
    return f"{category}_{secrets.token_hex(12)}"
