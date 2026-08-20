# Runs and cancels are requests

Part of [souk's mechanisms](../mechanisms.md).

Everything souk sends a provider is a request, never a command. souk
cannot make a provider take a run, and it cannot make one stop — it can
only ask, watch what happens, and record exactly that.

## Offering a run

A queued run is *offered* to its agent's provider — in order, one turn
per thread at a time: a run whose thread already has a run in flight
(claimed, or paused waiting for an answer) stays queued until that turn
ends, without holding up runs on other threads — and the provider
answers with one of three values:

- **accepted** — the run is claimed and its events start flowing;
- **declined** — "full right now"; souk keeps the run queued and offers
  again when something changes, for as long as the agent has a provider
  attached. Only an agent left *unserved* past a grace window (a broker
  argument, 45 s by default) has its queued runs given up on, failed as
  `no_provider_took_it` — a timeout on the absence of anyone to ask, not
  a judgment;
- **refused** — permanent: this provider will never accept it (an agent
  it no longer serves, an input that can never validate). souk fails the
  run with the provider's own reason recorded verbatim and stops
  re-offering. souk invents no reason of its own — the annotation is the
  provider's.

The three-valued answer exists because collapsing decline and refusal
into one bit left runs re-offered forever, reading as `queued` from every
vantage point while the provider's log alone knew the truth.

## Cancelling a run

A cancel is relayed to the provider as a request. The provider may comply,
finish normally anyway, or ignore it; souk keeps relaying whatever the
run emits and records the outcome it then observes — a claimed run is
never marked `cancelled` at request time, because souk never records an
outcome it has not observed. The one immediate cancellation is a run
still queued: no provider has it, so there is no outcome souk could be
pre-empting. The same rule holds everywhere: statuses are observations,
not intentions.

## What a run's final status is

One funnel settles every run that reached a provider
(`handlers._handle_finish`), and the conditions are tried **in this
order** — the first match wins:

| condition | status | recorded metadata |
|---|---|---|
| the stream ended on an interrupt outcome | `input-required` | the pause payload, interrupts preserved |
| the provider sent `RUN_FINISHED` | `completed` | — |
| a cancel had been requested | `cancelled` | — |
| none of the above | `failed` | `provider_stream_ended_without_finishing` |

The order is the mechanism, not an implementation accident. **A provider
that ignores a cancel and finishes is recorded `completed`**, because
`RUN_FINISHED` is tried before `cancel_requested` — souk asked, the
provider declined to stop, and the run's own output is what happened. A
pause outranks both: an interrupt outcome arrives *on* a `RUN_FINISHED`,
so the run that stopped to ask a human is not filed as one that finished.

Runs that never reach that funnel are failed with a reason instead: an
agent with no attached provider (`agent_offline`), a queued run whose
agent went unserved past its window (`no_provider_took_it`), a permanent
refusal, and an event that is not valid AG-UI.

A run recorded `failed` that never emitted its own `RUN_ERROR` gets one
synthesized, persisted and relayed, so a caller can tell failure from an
agent with nothing to say. A run that already reported its own is left
alone.

## Cancelling is not a state a caller can read as final

`cancelling` is an **active** status, alongside `queued`, `running` and
`input-required`. It means souk has passed the request on and is still
relaying — not that the run stopped. It has no A2A equivalent, because
A2A's `TASK_STATE_CANCELED` asserts an outcome souk has not observed
yet; only the settled `cancelled` maps to it.

While a run is cancel-requested, a KYOK completion call naming it is
refused. souk stops funding work it has been asked to stop, which is the
one consequence a cancel has that does not depend on the provider
agreeing to it.

## AG-UI has no cancel signal

Measured against the installed `ag-ui-protocol`: `EventType` has no
cancel member, and a run's terminal events are `RUN_FINISHED` and
`RUN_ERROR` only, whose outcome is success or interrupt. There is no
cancelled event and no cancelled outcome.

So the AG-UI door has no cancel entry point, and A2A's `tasks/cancel` is
the only cancel a standard client can send. souk does not invent one —
inventing an event type would be exactly the forced protocol deviation
the [integration contract](../integration-contract.md) rules out. It also
explains the asymmetry in what souk reports: a `failed` run gets a
terminal `RUN_ERROR` because AG-UI has one to send, and a `cancelled`
run gets nothing, because there is no such event and the only party who
would read it is the one who asked.

## Design records

Why this is shaped the way it is, and what it was shaped like first:

- [Silence about a verdict souk has reached is a bug](../design-records.md#silence-about-a-verdict-souk-has-reached-is-a-bug)
- [Enforcing cancellation produced a family of bugs](../design-records.md#enforcing-cancellation-produced-a-family-of-bugs)
