from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOUK_PACKAGE = Path(__file__).resolve().parent.parent / "souk"

FORBIDDEN_ROOTS = {
    "grpc",
    "fastapi",
    "uvicorn",
    "starlette",
    "sse_starlette",
    "httpx",
    "websockets",
}

def _core_modules() -> list[Path]:
    return sorted(
        path for path in SOUK_PACKAGE.rglob("*.py") if not path.name.startswith("__")
    )


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("module", _core_modules(), ids=lambda p: str(p.relative_to(SOUK_PACKAGE)))
def test_core_module_imports_no_transport(module: Path) -> None:
    offenders = _imported_roots(module) & FORBIDDEN_ROOTS
    assert not offenders, (
        f"{module.name} imports {sorted(offenders)}, which makes souk's core depend on a "
        "transport. Move whatever needs it into the serving layer, or put a port in front "
        "of it — see docs/core-components/dispatch.md."
    )


def test_generated_wire_stubs_are_not_reachable_from_core() -> None:
    offenders = [m.name for m in _core_modules() if "grpc_gen" in m.read_text()]
    assert not offenders, f"core modules referencing generated worker-channel stubs: {offenders}"


def test_no_transport_is_even_installable_as_a_dependency() -> None:
    pyproject = (SOUK_PACKAGE.parent / "pyproject.toml").read_text()
    declared = "\n".join(
        line for line in pyproject.split("[dependency-groups]")[0].splitlines()
        if not line.lstrip().startswith("#")
    )
    offenders = sorted(root for root in FORBIDDEN_ROOTS | {"sse-starlette"} if root in declared)
    assert not offenders, (
        f"souk's own dependencies include {offenders}. Serving belongs in the "
        "AgentSoukServer repo; a transport listed here puts it one import away from core again."
    )
