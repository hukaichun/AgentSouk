# openai-compat-agent

Wraps any OpenAI-compatible chat endpoint as a souk provider — a raw LLM,
or a full agent (tools, memory, its own multi-step reasoning) that just
happens to speak that wire format for convenience, e.g.
[Hermes Agent](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server)'s
own API server. No prompting/orchestration of its own beyond an optional
per-agent `system_prompt` — see `openai_compat_agent/main.py`'s module
docstring for the full contract, and
[docs/agent-provider-guide.md](../../docs/agent-provider-guide.md) for why
pause/resume is explicitly out of scope here.

## Two demos, two different setups

- **`config.example.yaml`** — three personas (`concise-bot`,
  `shakespeare-bot`, `emoji-bot`) all pointed at the *same* raw LLM (your
  `.env`'s `LLM_BASE_URL`/`LLM_API_KEY`/`LLM_MODEL_NAME`) with different
  `system_prompt`s. Needs nothing beyond `souk`/`paradedb` — this is the
  zero-extra-dependency way to see the wrapping mechanism work.
- **`config.hermes.yaml`** — one agent, a real
  [Hermes Agent](https://github.com/NousResearch/hermes-agent) instance
  (`nousresearch/hermes-agent:latest`), wrapped via its own OpenAI-compatible
  API server. This is the actual point of this package: proof that an
  entire pre-built agent (not just a raw model) can be dropped onto souk
  with zero code changes, just config.

## Running the Hermes demo

Off by default — it pulls a third-party image and runs a real agent with
terminal/file access, not something to start by accident:

```bash
docker compose --profile hermes-demo up -d --build hermes openai-compat-demo
```

(`souk`/`paradedb` come up automatically as dependencies.) Then:

```bash
curl -X POST http://localhost:8000/a2a/hermes/rpc \
  -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":"1","method":"SendMessage","params":{"message":{"role":"ROLE_USER","parts":[{"text":"hi"}]}}}'
```

### Configuration gotchas (all discovered the hard way — see git history)

- Hermes's `config.yaml` (`hermes-data/config.yaml`, bind-mounted into
  the container) supports `${VAR}` expansion for `base_url`/`api_key`,
  but **not** for `model.default` — that field must be a literal string,
  not a variable reference.
- Hermes's `AZURE_FOUNDRY_*` env vars (not the generic `custom` provider)
  are what correctly reach an Azure AI Foundry-style endpoint — using
  `provider: custom` against the same `base_url` produces a misleading
  `DeploymentNotFound` error instead of actually working.
- `API_SERVER_ENABLED` is not what gates Hermes's API server — it's
  gated purely on `API_SERVER_KEY` being ≥16 characters (see
  `gateway/config.py`'s `_has_usable_api_server_key`). A short/missing
  key means the platform silently never starts, with only "No messaging
  platforms enabled" in the logs to go on.
- `hermes-data/` ends up owned by uid 10000 (the container's internal
  `hermes` user), not your host user, the first time the container
  writes to it. If you need to edit `hermes-data/config.yaml` from the
  host afterward: `docker run --rm -v ./hermes-data:/data alpine chmod -R a+rwX /data`
  first.
- Without `session_id_header`/`memory_scope_header` set in
  `config.hermes.yaml` (souk's `threadId` forwarded as
  `X-Hermes-Session-Id`/`X-Hermes-Session-Key`), Hermes derives its own
  session id from the conversation's first message — two different souk
  threads can collide, and its long-term memory isn't scoped per caller
  at all. Verified this matters: a fresh souk thread correctly reports
  no memory of a secret shared in an unrelated thread only once these
  headers were wired up.

### Security note

Hermes logs this warning on startup, and it's correct, not boilerplate:
with the terminal backend left at `local` (this demo's default) and the
API server bound to `0.0.0.0` (needed for the `openai-compat-demo`
container to reach it), any agent work dispatched through this endpoint
runs with the Hermes container's own file/terminal access — fine for a
local demo, not for anything actually exposed. See Hermes's own docs on
`terminal.backend: docker` before using this pattern for real.
