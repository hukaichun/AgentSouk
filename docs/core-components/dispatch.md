# The dispatch trunk

Part of [core components](../core-components.md).

Four lanes, all network-free — each is a set of methods a serving layer
puts on whatever wire it chooses.

## The caller doors: AG-UI and A2A

`protocols/agui.py` and `protocols/a2a.py` are the adapters a standard
client's requests land on: start or resume a run, stream its events,
read a thread back, send an A2A message, follow task lineage. Both
validate against the protocol packages themselves (`ag-ui-protocol`,
`a2a-sdk`) — souk hand-writes no protocol, so a renamed field upstream
fails at import, not in production.

## The translation: A2A becomes AG-UI before dispatch

Every A2A call is translated into the same AG-UI-shaped run input the
AG-UI door produces (`build_run_agent_input`), so a provider sees one
shape regardless of which protocol the caller spoke — this is what lets
souk open an A2A door for an agent whose author never wrote a line of
A2A. souk's own forwarded-props additions are built in one place for
both doors, so a run's `caller` looks the same whichever road dispatched
it.

## The agent-provider lane: offer, claim, pipeline

`RunBroker` is the brokering machine: a started run queues per agent; the
broker offers it to the agent's attached provider inside a delivery
window; the provider answers accepted, declined, or refused (see
[runs and cancels are requests](../mechanisms/requests.md)); an accepted
run is claimed and gets a per-run pipeline that consumes its commands in
order — relay an event, finish the stream, request a cancel, fail — with
`handlers.py` folding each into the database and the caller's live
stream. Capacity is the provider's declaration; what souk observes about
it goes to the quality counters.

## The LLM-provider lane: the completion relay

`KyokRelay` plus `protocols/kyok.py` are the relaying machine: a
KYOK-bound run's agent calls the OpenAI-compatible door; three checks
authorize the call (souk's token, the run still live, the agent's own
signature); the binding resolves to whichever connection currently serves
the offering, and chunks stream back through a counter that records the
outcome. No queue, no negotiation — an unattached offering is an
immediate fast-fail, because the caller is waiting on a live stream.

## One substrate under both

A broker and a relay are deliberately different machines — one queues and
negotiates, one passes through — but each keeps the same roster: which
connection serves each ref, one connection per role, counters recorded
and never judged. That table is extracted once as `LiveRoster` and
composed by both, so the two lanes cannot drift apart; the register /
attach / detach semantics above them are likewise stated once, in the
facade's `_Roster`.
