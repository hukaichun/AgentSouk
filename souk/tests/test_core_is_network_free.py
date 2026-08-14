"""souk's core must not depend on any transport.

This is the invariant the whole library architecture exists to protect (see
docs/library-architecture.md): core knows about a database and nothing else,
so which protocol providers or callers arrive over is a serving-layer choice
rather than something baked into souk. That property is easy to state and
easy to erode — one convenient import of `grpc` or `fastapi` in a core module
and it is quietly gone — so it is asserted here rather than left to reviews.

If this fails, the fix is almost never "add the module to the allowed list".
It is that whatever needed a transport type belongs in the serving layer, or
needs a port (souk/providers.py is the example) so core can stay ignorant of
it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOUK_PACKAGE = Path(__file__).resolve().parent.parent / "souk"

# Transport/framework packages core may never import, directly or otherwise.
FORBIDDEN_ROOTS = {"grpc", "fastapi", "uvicorn", "starlette", "sse_starlette", "httpx"}

# The serving layer: allowed to import all of the above, and expected to.
# Everything else under souk/ is core. Kept as an explicit list so adding a
# module to it is a deliberate act with a diff, not an accident.
SERVING_MODULES = {
    "server.py",
    "deps.py",
    "grpc_server.py",
    "api_registry.py",
    "api_agui.py",
    "api_a2a.py",
    "api_llm_bridge.py",
}


def _core_modules() -> list[Path]:
    return sorted(
        path
        for path in SOUK_PACKAGE.glob("*.py")
        if path.name not in SERVING_MODULES and not path.name.startswith("__")
    )


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import, always within souk itself.
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("module", _core_modules(), ids=lambda p: p.name)
def test_core_module_imports_no_transport(module: Path) -> None:
    offenders = _imported_roots(module) & FORBIDDEN_ROOTS
    assert not offenders, (
        f"{module.name} imports {sorted(offenders)}, which makes souk's core depend on a "
        "transport. Move whatever needs it into the serving layer, or put a port in front "
        "of it — see docs/library-architecture.md."
    )


def test_grpc_generated_stubs_are_not_reachable_from_core() -> None:
    """protobuf leaks the same way an import of `grpc` does, and used to:
    three handlers built souk_pb2 envelopes directly. They now live in
    souk/handlers.py and talk through the AgentProvider port instead.
    """
    offenders = [m.name for m in _core_modules() if "grpc_gen" in m.read_text()]
    assert not offenders, f"core modules referencing generated gRPC stubs: {offenders}"


def test_the_serving_list_is_not_stale() -> None:
    """A serving module that no longer exists would silently widen what
    counts as 'core is allowed to skip'."""
    missing = sorted(name for name in SERVING_MODULES if not (SOUK_PACKAGE / name).exists())
    assert not missing, f"SERVING_MODULES lists modules that no longer exist: {missing}"
