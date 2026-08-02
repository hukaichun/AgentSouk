# souk-directory

A human-browsable directory for a souk: browse its registered agents and
chat with one directly from the browser.

TypeScript, compiled to plain ES modules with `tsc` (no bundler, no
framework), served as static files — no backend of its own, a pure
browser client of a souk's already-public HTTP API (`GET /agents`,
`POST /agui/id/{agent_id}`, A2A agent cards). Independent project, like
every other top-level directory in this repo: nothing here imports from
`souk/` or `souk-agent-sdk/`.

## Running

**Via `docker compose up --build`** (from the repo root) — this is already
one of the services in the top-level `docker-compose.yml`; once it's up,
open `http://localhost:8080/index.html`. No `?souk=` needed for the local
default: it falls back to `http://localhost:8000`, matching this repo's
own compose file.

**Standalone**, against any souk:

```bash
cd souk-directory
npm install
npm run build   # tsc — compiles src/*.ts to dist/*.js
python -m http.server 8080
```

Then open `http://localhost:8080/index.html?souk=http://wherever:8000`
(swap in whichever souk you want to browse). The souk URL is remembered in
`localStorage` after the first visit — the `?souk=` query param is only
needed to set or change it, and every link within the directory carries it
forward automatically.

`souk`'s `SOUK_CORS_ALLOW_ORIGINS` must permit this page's origin (defaults
to `*`, fine for local development — see `souk/souk/config.py`).

## Source layout

- `src/app.ts` — shared helpers (souk URL resolution, `fetch`/SSE
  wrappers) imported by both pages.
- `src/index.ts` — the listing page (`index.html`).
- `src/agent.ts` — the chat page (`agent.html`).
- `npm run build` compiles all three into `dist/`, which the HTML files
  load as `<script type="module">`. `dist/` and `node_modules/` are
  gitignored — always run `npm run build` after editing anything in
  `src/` (CI does this too, see `.github/workflows/ci.yml`'s
  `souk-directory` job, which just fails the build on a type error, not a
  full test suite — there's no backend logic here to unit test).
