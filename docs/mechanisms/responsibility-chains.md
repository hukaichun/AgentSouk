# Responsibility chains

Part of [funduq's mechanisms](../mechanisms.md).

**Status: design, not implementation.** The design is settled and
recorded; no code enforces it yet. This page states the mechanism;
[`design/responsibility-chains.md`](https://github.com/hukaichun/funduq/blob/d78d0638c0ec2126167240c62471651b5468d35b/design/responsibility-chains.md)
is the full record, including what already exists in code and what is
direction only.

## The problem

A user talks to a main agent; the main agent delegates to a sub-agent;
the sub-agent pauses for a human answer (`input-required`). Now: who may
answer? The main agent can see the paused state but structurally cannot
resume it. The user upstream may not know the sub-thread exists. The
sub-agent's own operator may be exactly the right person — or exactly the
wrong one. And whoever answers: what proves they were entitled to, and
what records that it was them?

## The mechanism

The right to act on a paused thread becomes an explicit, **per-edge**
property of the delegation tree. Each delegation edge can carry the
responsibility onward, break it (the sub-tree handles its own pauses), or
extend it (a party up the tree may answer down here) — and the decision
bundles three things that travel together: the **right** to act, the
**cost** of acting (whose resources a resume spends), and the
**visibility** needed to act (who gets to see the paused thread at all).
Identifiers are never credentials: knowing a thread id is not what
entitles a party to resume it.

Answering is an auditable act: who resumed, under which edge's authority,
is recorded — the same observed-not-asserted stance every funduq record
takes.

## Design records

Why this is shaped the way it is, and what it was shaped like first:

- [Rule zero: identifiers are never credentials](../design-records.md#rule-zero-identifiers-are-never-credentials)
- [Anonymity means the key is unlinked, not that there is no key](../design-records.md#anonymity-means-the-key-is-unlinked-not-that-there-is-no-key)
- [One question per delegation edge decides the whole tree](../design-records.md#one-question-per-delegation-edge-decides-the-whole-tree)
- [Authorization is not disclosure](../design-records.md#authorization-is-not-disclosure)
