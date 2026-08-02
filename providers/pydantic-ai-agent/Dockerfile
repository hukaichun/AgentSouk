# Build context is the repo root (only so proto/, scripts/, and
# souk-agent-sdk/ are reachable — pydantic-ai-agent is an independent
# project, not a workspace member; see the root pyproject.toml's
# comment). This image only ever copies souk-agent-sdk/,
# providers/pydantic-ai-agent/, proto/, and scripts/ — nothing from
# souk/, souk-client-sdk/, or agent-template/ ends up in it.
FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY proto /app/proto
COPY scripts /app/scripts
COPY souk-agent-sdk /app/souk-agent-sdk
COPY providers/pydantic-ai-agent /app/providers/pydantic-ai-agent

# Stubs must exist in souk-agent-sdk's own source tree *before* syncing
# pydantic-ai-agent below — it depends on souk-agent-sdk via a plain path
# source (see providers/pydantic-ai-agent/pyproject.toml), which builds a
# wheel from whatever's on disk at that point.
WORKDIR /app/souk-agent-sdk
RUN uv sync --group dev \
    && uv run bash ../scripts/gen_proto.sh souk_agent_sdk/grpc_gen

WORKDIR /app/providers/pydantic-ai-agent
RUN uv sync

ENV AGENT_TEMPLATE_CONFIG=/app/config.yaml

CMD ["uv", "run", "pydantic-ai-agent"]
