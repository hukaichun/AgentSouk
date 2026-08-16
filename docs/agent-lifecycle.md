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
   nothing is using and nothing ever used — a registration with no
   conversation behind it.

Retiring an agent and deleting one come apart, and only the first is a
lifecycle stage. Retiring is (1): stop offering it, and it goes offline and
eventually off the roster, with its record and everything it did intact.
Deleting only ever removes a registration that never became anything.

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

### What may be deleted: only an agent nothing has ever used

Refused unless all four hold. The first three are "nothing is using it right
now"; the fourth is "nothing ever did".

| check | why |
|---|---|
| not online (`last_seen_at` outside `online_window_seconds`) | a provider still checking in is still serving it |
| no attached in-process worker serving it (`Souk._workers`) | a wedged worker can be offline *and* attached; both are evidence |
| no active run (`queued`, `running`, `cancelling`, `input-required`) | the same set `get_active_run_for_thread` already treats as active |
| **no threads at all** | a conversation is not the provider's alone to destroy |

`input-required` is the one a narrower liveness check would miss: that run is
paused on a human who is coming back.

The fourth check is what makes `threads`' foreign key to `agents` real rather
than an obstacle. A thread must name an agent; therefore an agent with threads
cannot be removed. The constraint and the rule are the same statement.

Checking `threads` is sufficient for "any history": a run always lives in a
thread owned by the same agent (`ensure_thread` raises
`ThreadOwnershipMismatch` otherwise), so there is no way for `thread_history`
or `run_events` rows to exist for an agent with no threads. The
implementation should still check both — the cost is one query and the claim
above is an invariant, which is exactly the kind of thing that quietly stops
being true.

So the delete is a plain single-row `DELETE`, with no cascade, in every case
it is allowed to run. Three problems the earlier cascading design had are not
solved but *absent*:

- it cannot destroy a caller's messages, which souk stores deliberately so
  that it is "a source of truth for the full conversation, not just the
  caller's half of it" (`handlers._handle_finish`)
- it cannot break another agent's delegation lineage through
  `threads.parent_thread_id`, the self-referencing FK that made cascade order
  a judgement call rather than a rule
- there is no multi-statement, FK-ordered deletion whose behaviour differs
  per dialect

**What this method is actually for**, stated plainly because "delete an
agent" promises more than it delivers: clearing rows that were never used — a
typo in a name, a test registration, a batch pushed from the wrong config. It
is a tidy-up, not a lifecycle stage.

**Retirement is a different act, and it is the one that already works.** A
provider that no longer offers an agent stops including it; it goes offline
immediately, and after `stale_hidden_window_seconds` it drops off the roster
without being deleted. The row survives, along with everything it ever did.
That is the whole answer to "don't let a registered agent simply vanish" —
deletion is not the retirement path and does not need to be.

**Consequence to accept, not a gap:** an agent that has run once can never be
removed. It can only go quiet. Rows accumulate for the lifetime of the
deployment.

**Known gap until the broker work lands.** The active-run check reads this
process's broker plus the database; the online check reads the database only.
Across several souk processes (`docs/broker-horizontal-scaling.md`) a run live
on another node is visible in the database as `running`, so the DB half covers
it — but this is exactly the "one node cannot see another's live state"
question that document exists for, and this check should be revisited when the
run ownership columns land. The thread check is unaffected, being pure
database state, and it is the one guarding anything irreversible.

### Resurrection

Re-registering the same `(public_key, name)` afterwards produces a working
agent again. Under `docs/retiring-agent-id.md` that pair *is* the identity, so
it is the same agent by definition — and now trivially so, since the only
agents that can be deleted have no past to be missing.

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

The guard is now the entire feature — what is left of the delete is one row —
so that is where the tests go.

- Each refusal separately: online, attached, each active status, and
  has-threads. `input-required` above all, since that is the one a plausible
  narrower liveness check misses.
- **The has-threads refusal, tested through a real run rather than an
  inserted row.** Register, run something to completion, let the provider go
  offline, then try to delete: refused. Asserting it against a hand-inserted
  `threads` row would pass even if the check were wired to the wrong column,
  because the test would have built the state the check happens to read.
- The delete that *is* allowed: register, never use it, delete, and assert
  the row is gone and the name is registerable again.
- Domain separation gets the test that matters: take a *registration*
  signature for one agent and present it as a deletion. It must be refused.
  Written first, against the current payload, where it will **pass** — which
  is what proves the hole is real rather than theoretical.
- Both backends, per CLAUDE.md, though the dialect surface is now small: one
  `DELETE`, and the `delisted_at` column drop, which on SQLite is a table
  rebuild (`batch_alter_table`).
