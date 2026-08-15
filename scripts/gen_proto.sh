#!/usr/bin/env bash
# Regenerate gRPC stubs from proto/souk.proto.
#
# With no args: regenerates souk-agent-sdk/souk_agent_sdk/grpc_gen — the
# convenience path for local dev after editing proto/souk.proto (run from
# the repo root: `uv sync --group dev && ./scripts/gen_proto.sh`).
#
# With one or more explicit paths: generates into just those directories
# (relative to the current working directory) — used by consumers whose
# checkout layout differs from this repo's, e.g. the AgentSoukServer
# gateway, which invokes this script from the submodule with its own
# output path.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_DIR="$SCRIPT_DIR/../proto"

if [ "$#" -gt 0 ]; then
    OUTS=("$@")
else
    ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
    OUTS=("$ROOT/souk-agent-sdk/souk_agent_sdk/grpc_gen")
fi

for OUT in "${OUTS[@]}"; do
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
    echo "Generated gRPC stubs into $OUT"
done
