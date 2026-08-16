# Retiring `agent_id`: address an agent by whose it is and what it is called

Status: **design, not yet implemented.**

An agent's identity is `(provider, name)` — the provider being its Ed25519
public key, or the fingerprint that stands for it. `agent_id` is a surrogate
for that pair, minted by souk, and this document is about removing it.

## `agent_id` carries no information

It is not an identity that happens to be opaque. It is a cache key, and the
code says so:

```python
# repo.register_agents
agent_id = existing_ids.get(name) or new_id("agent")
```

Every registration looks the id up by `(public_key, name)` and reuses whatever
it finds. The pair is the input; the id is the output. Everything else agrees:

- `UNIQUE(public_key, name)` has been in the schema all along
- de-listing sweeps by `public_key`
- ownership — the check that stops one provider claiming another's work — is
  `get_agent_ids_for_public_key`, keyed by `public_key`
- `AgentSummary` already carries `public_key` and `name`

So souk already knows the natural key everywhere. `agent_id` is a second name
for something that was never anonymous.

### This was asked for once, and half-delivered

Commit `41d2bd4`, "Let core address an agent by whose it is and what it is
called", is the response to that request. It reads:

> `UNIQUE(public_key, name)` is the pair an agent_id is assigned per, and
> de-listing sweeps by public_key; **only the lookup was missing.**

It added `resolve_agent(provider, name)` and stopped. Framing the gap as a
missing *query* rather than as a surrogate that should not be in the contract
is why the surrogate is still the primary key, still what claiming names,
still what the SDK routes on, and still in the Agent Card's URL. Recording
that here because the same framing will be available next time.

The precedent it should have followed is one commit earlier: `9225a57`,
"A provider is its key: drop `sdk_client_id` entirely" — the same argument,
made one level up and then not carried down.

## What it costs, measured

**1. A provider's vocabulary is database-scoped, and it cannot rebuild it.**

`claim_work(token, agent_ids)` requires a provider to hold ids only souk can
mint and echo them back on every call. Replace the database — a restore, a
redeploy against a fresh one — and every id the provider holds is meaningless.
It cannot re-derive them; only souk can. This is issue #37's root: a provider
ran 30 minutes with a healthy container, clean logs and exit code 0, absent
from the roster entirely, because it was claiming for ids nobody recognized.

The fix parked on `claude/issue-37-nothing-owned` makes that audible
(`NothingOwned` instead of an empty list) and is worth keeping either way —
a database swapped under a live connection still needs someone to say so. But
it makes the silence audible; it does not remove the class.

**2. souk's own in-process worker cannot recover from it at all.** Found while
testing #37: re-registering against a replaced database mints *fresh*
agent_ids, and `Worker` serves the ids it was attached with, so
`attach_provider` has to be called again with the new ones. A remote SDK gets
recovery for free by re-registering on reconnect — the in-process one has no
equivalent, and the whole point of the worker model is that the two run the
same loop. With `(provider, name)`, both hold names, names never change, and
neither needs a recovery path.

**3. The SDK built a routing table on a misreading.**

```python
# souk_agent_sdk/client.py
# dispatch is keyed by agent_id, not name, since name is no longer a unique
# routing key (see UNIQUE(public_key, name))
```

The uniqueness scope is wrong. `name` is not unique *across providers*; within
one provider it is exactly unique, and that is the only scope this code is in
— the SDK knows its own key. So `_handle_by_id` exists to translate ids back
into the `{name: AgentHandle}` map the SDK already had. It can go.

**4. Caller-side references break for no reason.** The Agent Card advertises
`/a2a/id/{agent_id}`, and the SDK's own identity docstring notes that
delegation configs point at agent_ids and end up "talking to the orphaned
identity". A `(fingerprint, name)` address survives everything a
`(public_key, name)` registration survives, which is the point of an address.

## The decision: remove it outright

`agents` is keyed by `(public_key, name)`. The column goes, and so does every
use of it.

An earlier draft of this document recommended the opposite — keep `agent_id`
as a private surrogate, out of every contract but still the primary key — and
that was wrong for two reasons worth recording, because both are easy to make
again.

**The migration was cheaper, which is not a design argument.** "No schema
change" was the headline of the case for keeping it. That is a property of the
*change*, not of the *result*, and it was doing the persuading. Once separated,
the design question — is there any benefit to this column existing? — has an
answer, below, and it is no.

**Keeping it privately is worse than either endpoint, not a midpoint.** Making
it private still changes every contract (claiming, the port, registration,
addressing, the roster), so none of that work is avoided. What it adds is a
permanent second vocabulary: `repo.py` (43 references), `broker.py` (18),
`kyok.py` (10) and the rest go on carrying ids, every boundary gains a
`(key, name)` → id resolution step, and every function signature becomes a
question about which vocabulary it is in. Full contract cost, indirection
retained, translation layer added.

**And the one real benefit does not exist here.** A surrogate earns its keep
when the natural key can change — rename the row, keep the references. Checked
rather than assumed: souk has no rename, anywhere, deliberately. A name is
part of an agent's identity (`UNIQUE(public_key, name)`, an id assigned per
pair, de-listing by absence of the name from a batch), so changing a name
already produces a different agent. There is nothing for stability-across-
rename to protect.

What is actually load-bearing is smaller than the 245-reference figure
suggests: **one foreign key.** `threads.agent_id` is the only one — checked
against `schema.py`, where `thread_history.agent_id` turns out to carry no
`ForeignKey` at all, just a nullable column.

## What changes

Internal resolution already exists — `repo.resolve_agent(provider, name)`,
which accepts a public key or a fingerprint — so much of this is threading the
pair through and deleting the translation on the way.

### Schema

| table | today | after |
|---|---|---|
| `agents` | `agent_id` PK, `UNIQUE(public_key, name)` | PK `(public_key, name)`; the separate unique constraint goes with it, being the same thing |
| `threads` | `agent_id` FK → `agents.agent_id` | `(agent_public_key, agent_name)` composite FK |
| `thread_history` | `agent_id`, nullable, no FK | `agent_public_key` / `agent_name`, nullable, still no FK |

Storage grows by a 64-hex key per thread and per run_status row. Accepted
rather than optimised around: it also makes "whose agent is this thread for"
answerable without a join, which several queries currently take one for. If it
ever matters, `providers.fingerprint` is the 16-hex form and is already unique
— a later change, with a measurement behind it, not a guess now.

### Contracts

| surface | today | after |
|---|---|---|
| `Souk.claim_work(token, agent_ids)` | souk-minted ids | `names` — the token already carries the key |
| `Provider.run_stream(agent_id, run_input)` | id | `name`; the SDK drops `_handle_by_id` |
| `Souk.attach_provider(provider_id, provider, agent_ids)` | ids | names |
| `Registration.agent_ids: {name: id}` | ids handed out | names only, or nothing |
| `AgentSummary.agent_id` | exposed | dropped — `public_key` and `name` are already on it |
| A2A card / routes `/a2a/id/{agent_id}` | id in the URL | `{fingerprint}/{name}` |
| AG-UI addressing | id | same pair |
| KYOK token's `agent_id` claim | id | the pair |
| `broker._pending_by_agent` | keyed by id | keyed by the pair — there is no id left to key it by |

`claim_work` keeping the *shape* it has — a list of things this provider
serves — matters: the ownership filter stays exactly as load-bearing, it just
filters names against `(this key, name)` instead of ids against a set of ids.
Nothing about "a valid token for one provider must not claim another's agents"
changes, and that property needs its existing test to keep passing untouched.

Downstream, in this repo: `souk-agent-sdk` (4 files), `souk-directory` (4),
`providers/` (3). `souk-client-sdk` and `agent-template` have none.
`AgentSoukServer` is a separate repository and has not been surveyed — its
routes carry `/a2a/id/{agent_id}` today, so it changes with the addressing.

## Two things found on the way that are not this change

Both surfaced while working on #37; neither is fixed by retiring `agent_id`,
and both should be decided separately rather than folded in silently.

- **De-listing does not remove ownership.** `get_agent_ids_for_public_key`
  does not filter `delisted_at`, so a de-listed agent is still owned by its
  key and its provider keeps claiming for it — while `get_agent_by_id`,
  `resolve_agent` and `list_agents` all treat it as gone. Measured, after a
  test written on the assumption that it did timed out. Now resolved from
  the other end: `docs/agent-lifecycle.md` stops registration de-listing
  anything, which leaves `delisted_at` with no writer at all, so the
  inconsistent state stops existing rather than being made consistent.
- **`Souk.enqueue_run` is annotated `-> RunSnapshot` and returns a live
  `Run`.** Unrelated, already recorded in
  `docs/broker-horizontal-scaling.md`'s phase 0.

## Phasing

Each phase green on SQLite and Postgres, per CLAUDE.md.

Contracts first, schema last. The reverse order would mean carrying both
vocabularies at once — which is exactly the shape rejected above, and it does
not become acceptable just because it is temporary.

1. **Core resolution and the claim surface.** `claim_work` and
   `attach_provider` take names; ownership filtering moves to `(key, name)`;
   `Registration` stops handing ids back. Core-only, and the point at which
   #37's root cause is gone for the in-process worker.
2. **The provider port.** `run_stream(name, run_input)`; `souk-agent-sdk`
   routes on its existing `{name: AgentHandle}` map and deletes
   `_handle_by_id`. This is a wire change, so per CLAUDE.md it gets
   `docker compose up` with a real key, not just a green suite.
3. **Protocol addressing.** AG-UI and A2A address `(provider, name)`; the
   Agent Card advertises it. `AgentSummary.agent_id` goes. Standard clients
   must be unaffected — this is a URL spelling, not a protocol deviation
   (`souk-no-forced-protocol-deviation`).
4. **The column goes.** One migration: `agents` re-keyed to
   `(public_key, name)`, `threads`' foreign key rebuilt composite,
   `thread_history` given the pair. By now nothing outside `repo.py` and
   `broker.py` still names an id, so this is the last of it rather than the
   start.

   Verify the schema the way `d363d76` did when the migration chain was
   collapsed: build it both ways and compare every column, constraint and
   index on both backends. SQLite has no `ALTER TABLE ... DROP CONSTRAINT`,
   so the `threads` foreign key means a table rebuild there — Alembic's
   `batch_alter_table`, which is exactly the operation that behaves
   differently per dialect and so gets run on both rather than reasoned
   about.

Landing 1 and 2 unblocks merging `claude/issue-37-nothing-owned`, whose
recovery story gets much shorter: re-register, and the worker picks up on its
own, because what it serves never changed.

## How this gets verified

The failure this removes is "a provider's identifiers went stale underneath
it", so the check has to actually replace the database:

- A probe that registers a provider, starts a run, wipes souk's tables, has
  the provider re-register, and asserts it is serving again **with no
  re-attach and no new identifiers anywhere** — the case that is impossible
  today, measured in #37's test as needing a second `attach_provider`.
- `scripts/probes/probe_multiprocess.py` already stands up real processes
  against one database and can host it (see its `node.py`), so the probe is
  an addition rather than new machinery.
- The ownership test (`test_a_token_cannot_claim_another_providers_agents`)
  must keep passing with the smallest possible edit. If retiring the
  surrogate needs that test rewritten, the ownership model changed too, and
  that is a separate decision from this one.
