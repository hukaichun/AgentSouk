"""What this SDK reads off souk, written down so souk can check it.

A provider cannot import souk — that is the boundary — so the two agree by
duck typing: the loop reads `run.run_id`, `run.agent.name` and
`run.run_input` off whatever souk hands it when it delivers a run.

Duck typing across a repository boundary is a promise nobody is holding, and
it was broken once already: souk handed over its own dispatch object, whose
input field is called `input_json`, and the first real provider to be given
one died with an AttributeError on its first run. So the expectations are
data here, and souk's own suite asserts they still hold
(`souk/tests/test_provider_sdk_contract.py`). Same device as the signing
payloads: one side states it, the other checks it, and a change that breaks
the pair fails at merge time instead of at a customer.

There used to be a third entry, naming souk's refusals so the loop could
match them by class name. It went with the loop that asked souk for work:
this SDK makes no call souk can refuse, so a list of refusals it recognises
would be a list nothing consults — see `RECOGNISED_SOUK_ERRORS` in the
history, and `souk.errors.NothingOwned`, which was deleted with it.

Deliberately *not* generated from souk. Something derived from souk agrees
with souk by construction and therefore checks nothing.
"""

from __future__ import annotations

# Every field the loop reads off a delivered run. souk's `ClaimedRun` must
# have exactly these — not a superset, because a field appearing there that
# the SDK never reads is a field somebody meant to hand a provider and
# nothing does.
CLAIMED_RUN_FIELDS = frozenset({"run_id", "agent", "thread_id", "run_input"})

# What identifies the agent on a delivered run. The pair is the identity (see
# `library-architecture.md`); the loop routes on the name because within one
# provider a name is unique and the provider already knows its own key.
AGENT_FIELDS = frozenset({"provider_key", "name"})
