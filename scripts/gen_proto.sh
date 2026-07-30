#!/usr/bin/env bash
# Regenerate gRPC stubs from proto/souk.proto into both souk/ and souk-agent-sdk/.
# Run from the repo root: ./scripts/gen_proto.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROTO_DIR="$ROOT/proto"

for OUT in "$ROOT/souk/souk/grpc_gen" "$ROOT/souk-agent-sdk/souk_agent_sdk/grpc_gen"; do
    mkdir -p "$OUT"
    touch "$OUT/__init__.py"
    python -m grpc_tools.protoc \
        -I "$PROTO_DIR" \
        --python_out="$OUT" \
        --grpc_python_out="$OUT" \
        --pyi_out="$OUT" \
        "$PROTO_DIR/souk.proto"
    # grpc_tools emits `import souk_pb2` (absolute) — rewrite to a package-relative
    # import so the generated stub works when imported as part of these packages.
    sed -i 's/^import souk_pb2 as souk__pb2$/from . import souk_pb2 as souk__pb2/' "$OUT/souk_pb2_grpc.py"
done

echo "Generated gRPC stubs into souk/souk/grpc_gen and souk-agent-sdk/souk_agent_sdk/grpc_gen"
