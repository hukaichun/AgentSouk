"""souk's core must not depend on any transport.

This is the invariant the whole library architecture exists to protect (see
docs/library-architecture.md): core knows about a database and nothing else,
so which protocol providers or callers arrive over is a serving-layer choice
rather than something baked into souk. That property is easy to state and
easy to erode — one convenient import of `grpc` or `fastapi` in a core module
and it is quietly gone — so it is asserted here rather than left to reviews.

Since the serving layer moved out into the `souk-server` distribution, this
is enforced twice over: by packaging (souk does not depend on fastapi,
uvicorn, sse-starlette or grpcio, so an import would not even resolve) and by
this test, which still runs because packaging protects against the accident
and not against someone adding the dependency back.

**`httpx` is the exception, and this test is now the only thing holding it.**
souk depends on `a2a-sdk` for A2A's own types (see souk/protocols/
a2a_translate.py — hand-writing that protocol drifted two versions without
anything failing), and its base install brings httpx along. So `import httpx`
in a core module would now resolve perfectly well. Nothing but the assertion
below stops it.

If it fails, the fix is that whatever needed a transport type belongs in
souk-server, or needs a port (souk/worker.py is the example: a worker reaches
core through plain method calls, so core never learns what carried them) so
core can stay ignorant of it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOUK_PACKAGE = Path(__file__).resolve().parent.parent / "souk"

# Transport/framework packages core may never import, directly or otherwise.
FORBIDDEN_ROOTS = {"grpc", "fastapi", "uvicorn", "starlette", "sse_starlette", "httpx"}

def _core_modules() -> list[Path]:
    """Every module in the package. There is no allow-list any more: the
    serving layer is a different distribution (souk-server), so nothing under
    souk/ is exempt — including souk/protocols/, which is the code most
    tempted to reach for a framework type and must not."""
    return sorted(
        path for path in SOUK_PACKAGE.rglob("*.py") if not path.name.startswith("__")
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


@pytest.mark.parametrize("module", _core_modules(), ids=lambda p: str(p.relative_to(SOUK_PACKAGE)))
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
    souk/handlers.py and never talk to a worker at all — a worker pushes
    events in, and whatever carried them was peeled off before core saw it.
    """
    offenders = [m.name for m in _core_modules() if "grpc_gen" in m.read_text()]
    assert not offenders, f"core modules referencing generated gRPC stubs: {offenders}"


def test_no_transport_is_even_installable_as_a_dependency() -> None:
    """The packaging half of the same invariant. A module cannot import what
    the distribution does not depend on, so this is what makes the rule
    structural rather than a matter of everyone remembering it — and it is
    the reason the module scan above has no allow-list left to keep honest.
    """
    pyproject = (SOUK_PACKAGE.parent / "pyproject.toml").read_text()
    # Comments only, stripped: this file says "no fastapi here" in prose, and
    # a scan that cannot tell prose from a requirement would fail on it.
    declared = "\n".join(
        line for line in pyproject.split("[dependency-groups]")[0].splitlines()
        if not line.lstrip().startswith("#")
    )
    offenders = sorted(root for root in FORBIDDEN_ROOTS | {"sse-starlette"} if root in declared)
    assert not offenders, (
        f"souk's own dependencies include {offenders}. Serving belongs in souk-server; "
        "a transport listed here puts it one import away from core again."
    )
