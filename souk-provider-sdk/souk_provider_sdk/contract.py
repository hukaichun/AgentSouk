"""What this package requires of whoever wires it up, written down so they
can check it.

A provider cannot import souk — that is the boundary — and this package no
longer reaches the other way either. It used to: the loop read `run.run_id`,
`run.agent.name` and `run.run_input` off whatever souk handed it, and called
`souk.report_event` / `souk.finish_run` by name. Souk's model fields, method
names and argument order were all part of this package's interface with
nothing on either side saying so, and it broke exactly there — souk handed
over its own dispatch object, whose input field is called `input_json`, and
the first real provider to be given one died with an AttributeError on its
first run.

So the shapes below are this package's own, and an integrator maps onto them.
That is the trade: one adapter that knowingly names both sides, instead of
two codebases silently assuming each other. Souk's suite asserts it can still
build one (`souk/tests/test_provider_sdk_contract.py`), so a change that
breaks the pair fails at merge time instead of at a customer.

Deliberately *not* generated from souk, for the same reason the signing
payloads in `identity.py` are not: something derived from souk agrees with
souk by construction and therefore checks nothing.
"""

from __future__ import annotations

# Every field of a run as this package receives one. An adapter fills these
# from whatever its own side calls them; `run_id`, `agent_name` and
# `run_input` are the three the loop actually reads, and the rest are carried
# for the agent's benefit rather than the loop's.
DELIVERED_RUN_FIELDS = frozenset(
    {"run_id", "agent_name", "run_input", "thread_id", "metadata"}
)

# What the runtime reports back through, and with what. Stated as data so the
# other side can assert its own methods still line up with these arities
# rather than discovering it at the first event.
#
# Both synchronous: an agent must never wait on whatever is downstream, and
# `on_finish` is called while unwinding a cancellation, where an await would
# be interrupted before it arrived.
REPORT_CALLBACKS = {
    "on_event": ("run_id", "event"),
    "on_finish": ("run_id",),
}

# What souk's broker needs from a provider: who it is, how much it will take
# at once, how to give it a run, how to ask it to stop one. Souk's
# `ConnectedProvider` protocol is the same four.
#
# `SoukConnection` is the base class that supplies them, so a transport that
# forgets one now fails at construction. This list stays as data anyway,
# because it is the *cross-repo* check: souk's suite asserts its own protocol
# still asks for exactly these, and a fourth thing appearing there would
# otherwise be found by a provider in the field. `max_concurrent_runs` is why
# it is worth keeping — souk's own docstring calls this trio "three things",
# and a connection missing it constructs and attaches cleanly, then fails
# inside the broker at registration.
CONNECTED_PROVIDER_ATTRS = frozenset(
    {"public_key", "max_concurrent_runs", "deliver", "cancel"}
)


# Every key `repo.register_agents` reads off one entry in a registration
# batch. `AgentHandle` must be able to express all of them, and this is the
# assertion that says so.
#
# It is here because the pair silently disagreed. souk has read
# `agent_card_extra` and `metadata` since its first commit; the HTTP model
# that used to fill them left with the serving layer, and the `AgentHandle`
# written to replace it was modelled on AG-UI's answer to "what is an agent"
# rather than on souk's. `register_agents` defaults both with `.get(..., {})`,
# so the two missing keys were not an error — every agent registered through
# the SDK simply got a card of name and description, no skills, and was
# therefore invisible to discovery while looking perfectly healthy.
#
# The same shape of defect as the run fields above, arriving by the same
# route: a migration that carried one side across and not the other, with
# nothing comparing them.
REGISTRATION_FIELDS = frozenset({"name", "description", "agent_card_extra", "metadata"})
