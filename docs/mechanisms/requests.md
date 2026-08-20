# Runs and cancels are requests

Part of [souk's mechanisms](../mechanisms.md).

Everything souk sends a provider is a request, never a command. souk
cannot make a provider take a run, and it cannot make one stop — it can
only ask, watch what happens, and record exactly that.

## Offering a run

A queued run is *offered* to its agent's provider, and the provider
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

## Design records

Why this is shaped the way it is, and what it was shaped like first:

- [Silence about a verdict souk has reached is a bug](../design-records.md#silence-about-a-verdict-souk-has-reached-is-a-bug)
- [Enforcing cancellation produced a family of bugs](../design-records.md#enforcing-cancellation-produced-a-family-of-bugs)
