# Build context is the repo root (this is a uv workspace) — see docker-compose.yml.
FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock* /app/
COPY souk /app/souk
COPY souk-agent-sdk /app/souk-agent-sdk
COPY souk-client-sdk /app/souk-client-sdk
COPY agent-template /app/agent-template
COPY proto /app/proto
COPY scripts /app/scripts

RUN uv sync --group dev \
    && uv run bash scripts/gen_proto.sh

CMD ["uv", "run", "agent-template"]
