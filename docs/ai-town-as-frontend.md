# AI Town as souk's frontend

Status: **evaluation, nothing implemented.** Recorded so the reasoning —
and the measurements behind it — survive the session that produced it.

The subject is [a16z-infra/ai-town](https://github.com/a16z-infra/ai-town),
evaluated at `depth 1` in a scratch checkout. Every claim below is marked
either *measured* (a probe was run) or *read* (source or upstream docs),
per CLAUDE.md.

## The proposal

Render souk's roster as a market: **a provider is a stall, an agent is a
person in that stall.** A human walks the market, reads the signs, walks up
to a stall and talks to someone in it.

This is not a metaphor imposed on the data model. It is already the data
model. souk addresses an agent by *whose it is and what it is called*:

    resolve_agent(provider, name)   ->   7f3a91c2 / translator
                                          stall     person

`providers.fingerprint` is a stall number, `providers.display_name` is the
sign over it (nullable — "this identity never said", so an unsigned stall
shows its number), and `agents.name` is a person behind the counter. The
test fixture for `provider_name` has read `"Ada's Stall"` since before this
evaluation existed.

| souk | market | notes |
| --- | --- | --- |
| `providers.public_key` | the stallholder | Ed25519, the only id a provider has |
| `providers.fingerprint` | stall number | UNIQUE — see "Layout" below |
| `providers.display_name` | the sign | nullable; unsigned stalls show the number |
| `agents.name` | a person in the stall | one Player on one tile |
| `agents.last_seen_at` | open / shut | whole stall at once — see below |
| `max_claim` | how many customers at once | per *stall*, not per person |
| `threads.parent_thread_id` | going to ask another stall | the person walks over |

## What AI Town actually is (read)

- Convex serverless, TypeScript. **No long-lived process** — queries,
  mutations, actions, and crons (`crons.interval` takes seconds).
- The engine is single-threaded per world and **exclusively owns** world
  state. Outside components may only mutate it by submitting `inputs`
  (upstream `ARCHITECTURE.md` states this as an invariant).
- An agent's turn is a fire-and-forget `internalAction`:
  `Agent.tick` → `startOperation('agentGenerateMessage')` →
  `chatCompletion()` → **one string** → `agentSendMessage` mutation.
  `ACTION_TIMEOUT` is 120s.
- Players block each other: `blockedWithPositions` rejects a position
  within `COLLISION_THRESHOLD` of another player.

So AI Town asks exactly one thing of a character's mind: given a message,
return text within 120 seconds.

## The call souk answers (measured)

A probe stood up an in-process `Souk`, registered two agents under one
identity, attached a provider, and called `A2AAdapter.send_task`:

- Blocking request/response returning a full A2A Task, `state: completed`,
  text in `artifacts[].parts[].text`. Well inside the 120s budget.
- `contextId` carried back continues the same thread; `Task.id` is new each
  turn — which is what "one conversation, many turns" needs.
- One provider hosting two agents received the correct `agent_id` for each,
  on its own thread.

A Convex action reaches this with a single `fetch()` to
`POST /a2a/id/{agent_id}/rpc`.

**Not** by pointing `LLM_API_URL` at souk. That seam is the LLM layer;
souk is a broker, not a model. The thing to replace is
`agentGenerateMessage` as a whole.

### Streaming is not lost (measured)

Upstream `ARCHITECTURE.md` says messages "are updated very frequently (when
streamed out from OpenAI)". In this version they are not: `util/llm.ts` has
a streaming overload, but all six call sites (`agent/conversation.ts` ×3,
`agent/memory.ts` ×3) use the non-streaming form. souk's blocking
`send_task` is therefore a same-shaped drop-in. Streaming later means
pointing at `/agui/id/{agent_id}` and feeding `TEXT_MESSAGE_CONTENT` deltas
into the same write path.

## Where the residents come from

The engine owns world state, so the roster reaches it through `inputs` and
nothing else: a Convex cron reads `GET /agents`, groups by `public_key` into
stalls, and submits `join` / `leave`.

Two gaps in the current surfaces:

- **Appearance.** `Player.join` requires a `character` (a sprite from
  `data/characters.ts`); souk has no such concept. The right home is
  `agents.metadata` — the schema comment already scopes it as
  "souk-internal extension data … Not interpreted by souk itself" — and
  `repo.register_agents` does persist `agent.get("metadata", {})` (read).
  But `repo.list_agents` does not project it. Adding it is one line.

  Do **not** use `agent_card_extra`: that is merged into `agent_card`, which
  is served verbatim as the A2A Agent Card. A sprite name has no business on
  a protocol surface.

- **Position.** `Player.join` picks a random free tile with no way to
  specify one. Without a patch, every world restart has all stallholders
  sprinting from random spots back to their stalls. An optional `position`
  argument is about three lines.

## Layout: the stall number is the stall's location

Providers arrive dynamically, and a stall must not move between syncs.
`fingerprint` is a stable short hash of the public key and is UNIQUE in the
schema — a second key hashing to the same prefix is refused by the database
rather than by a check that could race. Hashing it to a grid slot gives a
layout that is stable by construction and collision-free for the same reason
the address is.

Stalls need not block movement, so they can be a coordinate plus a render
overlay and **the engine does not change at all**. People do block each
other, so a stall footprint must give each of its agents its own tile.

## Two clocks

AI Town's conversations are physically gated: invite → walkingOver →
participating, and the participants must be within `CONVERSATION_DISTANCE`.
A run arriving over A2A from outside cannot wait for a sprite to cross the
map. The two cases render differently:

- **Started in the market by a human** — full physical flow. Walking over to
  a stall is the natural form of the request.
- **Started outside** — must not enter the conversation mechanism. Render it
  as `player.activity` (`{description, emoji, until}`), an existing
  primitive: the person shows as serving a request.

## The one real decision: stallholders do not wander

AI Town's `Agent.tick` decides on its own to go find someone to talk to. If
each character's mind is a real souk agent, **the town manufactures traffic
and spends real inference budget on NPC small talk.** For an
enterprise-internal deployment that is disqualifying.

An earlier draft of this evaluation proposed keeping the wandering (movement
costs nothing — `agentDoSomething` has a branch that only picks a
destination and calls no LLM) while removing conversation initiation. Under
the stall model that is still wrong: a stallholder stands at their stall.

**Characters do not move on their own at all.** What moves is customers —
humans, and agents delegating to another stall. Market activity is then
exactly real traffic, with no fake motion anywhere.

Three things fall out of that one cut:

1. No manufactured traffic.
2. **The thread-model mismatch disappears.** A souk thread belongs to *one*
   agent (`threads.agent_id` is NOT NULL); an AI Town conversation has
   *two* participants. That only collides for agent↔agent chat. Once every
   conversation is human↔agent or a real delegation, souk's model fits.
3. `convex/agent/` (955 lines of prompt engineering, memory, embeddings)
   and `Agent.tick`'s decision loop are both deletable. AI Town is reduced
   to a physical world and a renderer.

## The stall is the unit of capacity

From `souk/worker.py`:

> Claiming and concurrency belong to the provider as a whole, not to any one
> of its agents.

So a stall has one shared queue. `max_claim=2` with three callers means one
waits outside. Backpressure becomes something you can see rather than a
number in a log.

Presence has the same granularity: `claim_work` refreshes `last_seen_at` for
**every agent the worker hosts** (read). All of a provider's agents go
online and offline together — there is no half-staffed stall. A stall opens
or shuts as a unit.

## The self-delegation deadlock draws itself

souk documents, and deliberately does not fix, a deadlock: with
`max_claim=1`, an agent delegating to another agent *the same provider
hosts* hangs — the outer run holds the only slot and the inner one is never
claimed. Reproduced here independently: the outer run sat at `running` and
the agent was entered exactly once (measured).

On a market plan this needs no explanation:

> You are talking to someone in the stall. They turn to ask their colleague —
> but the stall serves one customer at a time, and that customer is you.

This is the strongest argument for the town as a frontend. An invariant that
otherwise takes three passages of prose and a commit message becomes
obvious, and the reason it is left unfixed becomes obvious with it: capacity
is the stall's statement about itself, not something souk should route
around.

Delegation to a *different* stall is the opposite — the person leaves their
counter and walks across the market. A delegation chain renders as a
journey. This is the one thing a market shows that a roster list cannot.

## Why not the other two directions

**Characters as souk providers** (so outsiders could call into the town) is
blocked on souk's side, not AI Town's. A provider is pull-based
(`claim_work`), and its only remote transport is the gRPC `PollForWork`
long poll — there is no HTTP claim endpoint. A Convex action cannot open
gRPC and cannot hold a poll loop; crons are minute-granular at best. Doing
it would need an external bridge process, and that bridge — not AI Town —
would be the provider.

**KYOK** looks like a zero-change option, since `/kyok/v1/chat/completions`
presents as an OpenAI-compatible host. It is not: a KYOK token names a run
and is paired with a call-time signature (`souk/protocols/kyok.py`), so AI
Town would have to already be a provider inside a run. It depends on the
direction above.

The frontend framing needs neither, which is why it is the cheap one.

## Scope

**AI Town**

- add: roster sync cron — `GET /agents`, group by `public_key`, submit
  `join` / `leave`
- add: stall overlay and fingerprint-derived layout
- add: externally-started runs render as `player.activity`, outside the
  conversation mechanism
- rewrite: `aiTown/agentOperations.ts` — LLM call becomes a souk A2A fetch
- patch: `Player.join` accepts an optional position
- delete: `convex/agent/`, and `Agent.tick`'s decision loop

**souk**

- one line: project `metadata` in `repo.list_agents`
- nothing else

## Known gap

**The people in the market are anonymous to souk.** A human walking up and
talking is a run whose caller is that human, but user identity is not built
(user identity is not provider identity). Such a run carries no
`actor_chain`. Not a blocker — but if "the delegation chain starts with a
person" is what the market is meant to show, that first link is missing.

## Evidence

| claim | basis |
| --- | --- |
| `send_task` blocks and returns a full Task; `contextId` continues a thread | measured (probe) |
| one provider, two agents, correct `agent_id` routing | measured (probe) |
| self-delegation deadlock: outer run at `running`, agent entered once | measured (reproduced) |
| all six `chatCompletion` call sites are non-streaming | measured (grep) |
| `claim_work` refreshes `last_seen_at` for every agent a worker hosts | read |
| the engine exclusively owns world state; `inputs` is the only way in | read (upstream `ARCHITECTURE.md`) |
| players block each other, so a stall needs one tile per agent | read |
| `repo.register_agents` persists `metadata`; `list_agents` does not project it | read |
| cross-stall delegation rendering, external-run activity path | not implemented |
