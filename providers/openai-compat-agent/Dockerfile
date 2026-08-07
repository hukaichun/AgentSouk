# Build context is the repo root (only so proto/, scripts/, and
# souk-agent-sdk/ are reachable — openai-compat-agent is an independent
# project, not a workspace member; see the root pyproject.toml's
# comment). This image only ever copies souk-agent-sdk/,
# providers/openai-compat-agent/, proto/, and scripts/ — nothing from
# souk/, souk-client-sdk/, or other providers/ ends up in it.
FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY proto /app/proto
COPY scripts /app/scripts
COPY souk-agent-sdk /app/souk-agent-sdk
COPY providers/openai-compat-agent /app/providers/openai-compat-agent

# Stubs must exist in souk-agent-sdk's own source tree *before* syncing
# openai-compat-agent below — it depends on souk-agent-sdk via a plain
# path source (see providers/openai-compat-agent/pyproject.toml), which
# builds a wheel from whatever's on disk at that point.
WORKDIR /app/souk-agent-sdk
RUN uv sync --group dev \
    && uv run bash ../scripts/gen_proto.sh souk_agent_sdk/grpc_gen

WORKDIR /app/providers/openai-compat-agent
RUN uv sync

ENV OPENAI_COMPAT_CONFIG=/app/config.yaml

CMD ["uv", "run", "openai-compat-agent"]
