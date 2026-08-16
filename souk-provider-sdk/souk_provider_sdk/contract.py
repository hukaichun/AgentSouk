"""What this SDK reads off souk, written down so souk can check it.

A worker cannot import souk — that is the boundary — so the two agree by
duck typing: the loop reads `run.run_id`, `run.agent.name`, `run.run_input`
off whatever `claim_work` hands back, and recognises souk's refusals by the
*name* of the exception class.

Duck typing across a repository boundary is a promise nobody is holding.
Rename a field and the loop breaks at runtime; rename an exception and it
breaks worse than that — `type(exc).__name__ == "NothingOwned"` simply stops
matching, the worker falls through to "unknown error, retry", and a provider
goes back to spinning silently forever. That is issue #37 returning, with
nothing turning red anywhere.

So the expectations are data here, and souk's own suite asserts they still
hold (`souk/tests/test_provider_sdk_contract.py`). Same device as the signing
payloads: one side states it, the other side checks it, and a change that
breaks the pair fails at merge time instead of at a customer.

Deliberately *not* generated from souk. Something derived from souk agrees
with souk by construction and therefore checks nothing.
"""

from __future__ import annotations

# Every field the loop reads off a claimed run. souk's `ClaimedRun` must have
# exactly these — not a superset, because a field appearing here that the SDK
# never reads is a field somebody meant to hand a provider and nothing does.
CLAIMED_RUN_FIELDS = frozenset({"run_id", "agent", "thread_id", "run_input"})

# What identifies the agent on a claimed run. The pair is the identity (see
# docs/retiring-agent-id.md); the loop routes on the name because within one
# provider a name is unique and the provider already knows its own key.
AGENT_FIELDS = frozenset({"provider_key", "name"})

# souk's refusals this worker acts on, by class name.
#
# NothingOwned: nothing this provider asked for is registered, so waiting is
#   futile — stay alive, say so, and resume if the names come back.
# InvalidRegistration: the session token is void — renew it and carry on.
#
# Anything not in here is retried as an unknown failure, which is the right
# default and the reason a rename must be caught here rather than in
# production.
RECOGNISED_SOUK_ERRORS = frozenset({"NothingOwned", "InvalidRegistration"})
