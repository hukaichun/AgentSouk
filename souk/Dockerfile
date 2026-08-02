# Build context is the repo root (only so proto/ and scripts/ are
# reachable — souk itself is an independent project, not a workspace
# member; see the root pyproject.toml's comment). This image only ever
# copies souk/, proto/, and scripts/ — nothing from souk-agent-sdk,
# souk-client-sdk, agent-template, or providers/ ends up in it.
FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY proto /app/proto
COPY scripts /app/scripts
COPY souk /app/souk

WORKDIR /app/souk
RUN uv sync --group dev \
    && uv run bash ../scripts/gen_proto.sh souk/grpc_gen

EXPOSE 8000 50051

CMD ["uv", "run", "souk-server"]
