# Build context is the repo root, because this image needs three sibling
# directories: souk-server itself, souk (its one path dependency — see its
# pyproject's [tool.uv.sources]), and proto/ + scripts/ to generate the gRPC
# stubs. Nothing from souk-agent-sdk, souk-client-sdk, agent-template or
# providers/ ends up in it.
FROM python:3.12-slim

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY proto /app/proto
COPY scripts /app/scripts
COPY souk /app/souk
COPY souk-server /app/souk-server

WORKDIR /app/souk-server
RUN uv sync --group dev \
    && uv run bash ../scripts/gen_proto.sh souk_server/grpc_gen

EXPOSE 8000 50051

CMD ["uv", "run", "souk-server"]
