# Working on funduq

Notes that were expensive to learn. Everything here comes from a mistake
actually made in this repo, not from general principle.

## Report the defect, not who spotted it

Don't narrate credit — "you were right", "good intuition", "you spotted it".
Even when true, it's the wrong thing to lead with: it centres the person
instead of the problem, carries no information the reader can use, and reads
as flattery once repeated. It's also a soft way of avoiding your own error.

Say the finding plainly:

> ✗ Your intuition was right, the design was wrong here.
> ✓ `start()` doesn't pass `agent_id`, so a provider serving two agents can't
>   tell its runs apart. `bind_run` was a workaround I added for that.

Who raised it is obvious from context.

Related: don't fold to a review comment just because it was made. Disagreeing
and then checking is what surfaces the real reason. One review here said a
routing table "should be in core"; the first answer was no, and only drawing
the actual flow showed it *should* move — but for a different reason than the
one given. Agreeing immediately would have skipped the diagram and missed it.

## Verify by running something

Nearly every real defect found in this repo was found by a throwaway probe
script, not by reading code. Reading produced confident wrong answers several
times over.

- an in-process provider reported `online: False` and calls to it fast-failed
  while it sat attached — found by listing the roster, not by inspecting
  `attach_provider`
- providers could be attached with no identity proof at all
- one provider attached to two agents couldn't tell its runs apart
- AG-UI has no cancelled event or outcome — checked against the installed
  package before designing around it, which changed the design
- `docker compose up` didn't start, a provider failure reached callers as an
  empty 200, and A2A answered `-32601` to every spec-current client. All
  three were live the whole time 204 tests were green — see Testing below
- funduq's own logging was disabled for the entire test session, and had been
  for a long time. `alembic/env.py` calls `fileConfig`, whose default is
  `disable_existing_loggers=True`; conftest migrates after importing funduq, so
  every `funduq.*` logger created by then was switched off. Found because a
  test asserting a logged warning failed while a throwaway script proved the
  code logged it — reading the code would never have found this

When you catch yourself about to write "this should work" or "X is
transport-specific", write eight lines that prove it instead.

## Testing

- Run the suite on **both** backends. SQLite is the default;
  `FUNDUQ_DATABASE_URL=postgresql+psycopg://…` for the other. A throwaway
  Postgres container is enough. Dialect bugs only appear on one side.
- **A green suite does not mean the app starts.** The lesson came from the
  serving layer (now the funduq-server repo): nothing imported its
  `server.py` at test time, and a rename sweep once left an import there
  that doesn't even parse, with 167 tests passing. The app-builds probe
  lives in that repo's CI now; the lesson applies to any bootstrap no test
  imports.
- `tests/test_core_is_network_free.py` is a hard constraint, not a
  suggestion. If it fails, the fix is almost never to widen its allow-list.
- **Every test provider is a stub, so nothing here proves funduq works.** The
  suite has never called a model. The check that does is the demo stack —
  `docker compose up` with a real key in `.env` — which now lives in
  funduq-server (this repo's compose carries only a Postgres); the first
  time it was run it found three defects in a row: the stack wouldn't start
  (host `.venv` copied into the image, so `uv` re-downloaded everything at
  container start), a failing provider reached callers as a 200 with zero
  events, and A2A only answered to method names the spec had renamed. Run
  it from there after touching the wire.
- **A protocol funduq hand-writes will silently rot, and reading the package
  is not enough on its own — check *which version* you are reading.** A2A had
  moved twice; the first fix landed on v0.3 because its shapes were read out
  of a module called `a2a.compat.v0_3` without asking what it was
  compatibility *for* (answer, in its own README's first line: for v1.0
  systems talking to legacy v0.3 ones). Both protocols now come from a
  package — `ag-ui-protocol` and `a2a-sdk` — and A2A's method names are read
  off the `A2AService` descriptor so a rename fails at import. Keep it that
  way: no A2A field name, enum value or method name gets typed by hand.

## Design invariants

These are load-bearing; breaking one has caused a real bug here.

- **Core is network-free — including its vocabulary.** It knows a database
  and nothing else. Which protocol something arrives over is a serving-layer
  choice, so core must not *name* one either: `broker.py`, `identity.py`,
  `repo.py` and `kyok.py` all described themselves in terms of `PollForWork`
  and `AgentSession` until the transport changed and every one of those
  sentences became a lie. The contract is `claim_work` / `report_event` /
  `finish_run` plus a cancel notification; a transport frames those.
- **funduq never decides on a provider's behalf.** It can *ask* an agent to
  stop; it cannot make it. Never record an outcome funduq hasn't observed —
  recording `cancelled` at request time was a lie the run's own output could
  contradict.
- **In-process is not trusted.** Sharing a process is not a reason to skip
  registration, identity, or liveness. Any shortcut for in-process that a
  remote provider doesn't get is a bug in the making.
- **Don't force protocol deviations.** A standard AG-UI or A2A client must
  work unmodified. If funduq seems to need a new field or endpoint, check
  whether the protocol already has one — see `funduq-no-forced-protocol-deviation`.

## Where the design lives

`docs/design-records.md`. It records decisions *and* the ones that turned
out wrong, with the measurements behind them. Read it before changing the
provider port, cancellation, or the core/serving boundary — and if the code
contradicts it, one of them needs fixing, deliberately.

The `design/` directory it was distilled from is gone; each record links
into it at the commit before removal. **Do not treat those notes as
current.** They state designs in the present tense whether or not the code
followed, and five were falsified against the code in one sitting — the ack
signature, `A2AAdapter`'s arguments, `RunHandle.is_live`, two settings that
never existed, and a whole event-typing mitigation that was never built.
Grep for the identifier before repeating anything they say.
