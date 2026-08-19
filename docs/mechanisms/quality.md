# Provider quality counters

Part of [souk's mechanisms](../mechanisms.md).

Capacity is a provider's own declaration — `max_concurrent_runs` is its
word, unverified. What souk does instead of verifying is count what it
then observes, per provider, and judge nothing. The counters are the
material a serving layer, an operator, or a selection policy acts on;
souk itself draws no conclusion from them.

## What is counted

For an agent provider, the discourtesies souk can see from where it
stands: declining a run while claiming to have room (**misdeclared**),
not answering an offer inside the delivery window (**unanswered**),
taking a run and never finishing it (**abandoned**), answering an offer
after souk gave up waiting (**answered late**).

For an LLM provider, the fate of each relayed completion: streamed to the
end (**completions**), ended in a structured refusal (**refused**), died
with anything else (**failed**).

## The stance

Counting is the other half of "souk never records an outcome it has not
observed": what souk *does* observe, it writes down. Nothing here is a
penalty — a refused completion may be the provider's policy working
exactly as intended — and souk attaches no consequence to any counter.
Whether a count means "avoid this provider" or "this provider's policy is
strict" is the reader's judgment, made with context souk does not have.
