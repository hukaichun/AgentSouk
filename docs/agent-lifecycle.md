# Agent lifecycle: registering, going quiet, and being deleted

Status: **design, not yet implemented.** Independent of
`docs/retiring-agent-id.md` — this would be worth doing whichever way that
went — but the two touch the same functions and the delete method addresses
an agent the same way, so they should land together.

## What changes

Today a registration batch is a declarative statement of everything an
identity offers, and anything missing from it is de-listed on the spot. Three
changes:

1. **Absence from a batch means offline, not gone.** An agent a provider
   registered stays registered.
2. **Going quiet is the only thing absence can do.** `online` is already
   derived from `last_seen_at`, so this needs no new state.
3. **Deleting is its own explicit act**, and only possible for an agent
   nothing is currently using.

## Why absence-means-de-listed was wrong

The reasoning is recorded in `repo.register_agents`: the batch is the full
statement of what this identity offers, which "makes a plain re-registration
call the entire de-listing UX, no separate endpoint needed."

That is a convenience argument, and it buys the convenience with a failure
mode of exactly the shape issue #37 documents: **silent, and indistinguishable
from healthy.** A provider that starts with a partial agent list — a config
error, a feature flag off, half a deploy, a `for` loop over the wrong
collection — de-lists everything it failed to mention. Nothing fails. Its logs
are clean. The agents simply stop being findable, and the provider goes on
believing it is serving them (ownership does not consult `delisted_at`, so it
even goes on claiming for them successfully).

An explicit method cannot be reached by accident. That is the whole argument.

## The part that got simpler: `delisted_at` disappears

Not a cost of this change — a consequence, and worth stating because it points
the other way from what "add a delete method" sounds like.

`delisted_at` has exactly one writer: the omission sweep in `register_agents`
(and the `delisted_at=None` in the same function that clears it again). With
absence meaning offline and deletion being a real delete, **nothing writes it
any more.** The column goes, and with it six `delisted_at.is_(None)` filters
across `repo.py`.

Two states collapse into one. An agent exists or it does not; if it exists,
`online` is `last_seen_at` measured against the window, which is what every
consumer already asks. And the inconsistency found while working on #37 —
de-listed agents are hidden from `get_agent_by_id`, `resolve_agent` and
`list_agents` but still *owned*, so their provider keeps claiming for them —
dissolves rather than needing its own fix. There is no de-listed state left to
be inconsistent about.

## `delete_agent`

```python
await souk.delete_agent(public_key, name, signature, timestamp)
```

Signed, the same way registration is, and for the same reason: an agent
belongs to a keypair, and being in souk's own process is not evidence of
holding it (`docs/library-architecture.md`, "In-process is not trusted").
`InvalidRegistration`'s sibling case — a bad signature or a stale timestamp —
is refused identically.

### The signing payload needs domain separation, and today's does not

```python
def registration_signing_payload(agent_names: list[str], timestamp: int) -> bytes:
    return f"{','.join(sorted(agent_names))}:{timestamp}".encode()
```

A deletion payload of `f"{name}:{timestamp}"` would be **byte-identical to a
single-agent registration's**. Anyone who observed a provider register one
agent would hold a valid signature for deleting it, within the freshness
window. That is not a flaw introduced by adding deletion; it is one this
payload always had — nothing else was signed with it, so there was nothing to
confuse it with.

So both payloads get an operation prefix:

```python
b"souk-register:" + names + b":" + timestamp
b"souk-delete-agent:" + name + b":" + timestamp
```

This breaks the registration wire for any SDK that signs the old shape.
`docs/retiring-agent-id.md` phase 2 breaks it anyway, and souk is unreleased —
so this rides along there rather than being deferred into a version where it
costs more.

Freshness still bounds replay of a *deletion* signature the same way it does a
registration's: a captured one is usable until the timestamp goes stale, which
is why `is_timestamp_fresh` applies here unchanged.

### "Nothing is currently using it"

Refused unless all three hold:

| check | why |
|---|---|
| not online (`last_seen_at` outside `online_window_seconds`) | a provider that is still checking in is still serving it |
| no attached in-process worker serving it (`Souk._workers`) | a wedged worker can be offline *and* attached; both are evidence |
| no active run (`queued`, `running`, `cancelling`, `input-required`) | the same set `get_active_run_for_thread` already treats as active |

`input-required` matters most and is the one a narrower check would miss: that
run is paused on a human who is coming back, and deleting it destroys
something nobody has finished with.

**Known gap, until the broker work lands.** The active-run check reads this
process's broker plus the database; the online check reads the database only.
Across several souk processes (`docs/broker-horizontal-scaling.md`), a run
live on another node is visible in the database as `running` — so the DB half
covers it — but this is exactly the kind of "one node cannot see another's
live state" question that document exists for, and the check should be
revisited when the run ownership columns land.

### What deleting destroys

A hard delete, cascading, in child-first order (the FKs give no choice about
the order and SQLite has no `TRUNCATE ... CASCADE`):

1. `run_events` for the agent's runs
2. `thread_history` rows — its runs *and* the messages in its threads
3. child threads elsewhere whose `parent_thread_id` points into this set:
   **`parent_thread_id` set to NULL**, not deleted (see below)
4. `threads` owned by the agent
5. the `agents` row

**This destroys callers' messages, not only the provider's output.** souk
stores both halves of a conversation deliberately — `handlers._handle_finish`
persists agent replies precisely so souk is "an actual source of truth for the
full conversation, not just the caller's half of it" — so a thread is as much
the caller's record as the provider's, and this is a provider unilaterally
deleting it. That is the chosen behaviour and is what the offline guard exists
to bound; it is written down here so nobody discovers it from a support
ticket. `delete_agent` returns what it destroyed (threads, messages, runs,
events) rather than a bare bool, so the act is at least legible to whoever
performed it.

**Step 3 is the one real judgement call.** `threads.parent_thread_id` is a
self-referencing FK recording delegation lineage, so another agent's thread
can point at one of this agent's. Deleting the parent would either violate
that FK or cascade into a conversation belonging to somebody who was not
mentioned in this request. Nulling the pointer loses the lineage record — the
deleted agent is gone, so the lineage was going to be unreadable anyway — and
keeps the other agent's conversation intact. Destroying data nobody asked
about is the worse of the two.

### Resurrection

Re-registering the same `(public_key, name)` afterwards produces a working
agent again. Under `docs/retiring-agent-id.md` that pair *is* the identity, so
this is the same agent by definition — with no history, because the history
was deleted. Both halves of that sentence are true at once and neither is a
bug: the name was always this key's to offer, and deletion was always meant to
remove the record.

Worth being explicit because the two answers pull in different directions if
read quickly: soft deletion would have restored an identity *and* its past;
hard deletion restores the identity only.

## Phasing

Lands with `docs/retiring-agent-id.md`, whose phases these slot into.

1. **Stop de-listing on omission** (that document's phase 1, same function).
   Absence backdates `last_seen_at` the way `detach_provider` already does —
   witnessed departure, immediate, rather than waiting out the window.
2. **Delete `delisted_at`** and its six filters. Its own migration, and a
   small one: drop a column nothing writes.
3. **`delete_agent`**, with the domain-separated payloads (that document's
   phase 2, which is already breaking the registration wire).

## How this gets verified

- The guard is the part with something to get wrong, so each refusal gets a
  test: online, attached, and each active status separately —
  `input-required` above all, since that is the one a plausible narrower
  implementation misses.
- The cascade gets a probe rather than only a unit test: build a delegation
  chain (agent A's thread parenting agent B's), delete B, and assert A's
  thread survives with a NULL parent — on both backends, since this is
  multi-statement FK-ordered deletion and that is where dialects differ.
- Domain separation gets the test that matters: take a *registration*
  signature for one agent and present it as a deletion. It must be refused.
  Written first, against the current payload, where it will pass and prove
  the hole is real.
