# Build context is the repo root (only so proto/, scripts/, and
# souk-agent-sdk/ are reachable — agent-template is an independent
# project, not a workspace member; see the root pyproject.toml's
# comment). This image only ever copies souk-agent-sdk/, agent-template/,
# proto/, and scripts/ — nothing from souk/, souk-client-sdk/, or
# providers/ ends up in it.
FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY proto /app/proto
COPY scripts /app/scripts
COPY souk-agent-sdk /app/souk-agent-sdk
COPY agent-template /app/agent-template

WORKDIR /app/souk-agent-sdk
RUN uv sync

WORKDIR /app/agent-template
RUN uv sync

CMD ["uv", "run", "agent-template"]
