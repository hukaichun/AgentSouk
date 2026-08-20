from __future__ import annotations

import ast
from pathlib import Path

import pytest

FUNDUQ_PACKAGE = Path(__file__).resolve().parent.parent / "funduq"

FORBIDDEN_ROOTS = {"funduq_provider_sdk", "funduq_llm_provider_sdk", "funduq_client_sdk"}


def _core_modules() -> list[Path]:
    return sorted(FUNDUQ_PACKAGE.rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("module", _core_modules(), ids=lambda p: str(p.relative_to(FUNDUQ_PACKAGE)))
def test_core_module_imports_no_sdk(module: Path) -> None:
    offenders = _imported_roots(module) & FORBIDDEN_ROOTS
    assert not offenders, (
        f"{module.name} imports {sorted(offenders)}. The SDKs model parties that run "
        "in other processes; funduq shares only the wire contract with them, computed "
        "independently on each side so this suite stays a second opinion on the "
        "bytes. Re-state the contract in funduq instead of importing an SDK's copy."
    )


def test_no_sdk_is_even_installable_as_a_dependency() -> None:
    pyproject = (FUNDUQ_PACKAGE.parent / "pyproject.toml").read_text()
    declared = "\n".join(
        line
        for line in pyproject.split("[dependency-groups]")[0].splitlines()
        if not line.lstrip().startswith("#")
    )
    offenders = sorted(
        root for root in FORBIDDEN_ROOTS if root.replace("_", "-") in declared
    )
    assert not offenders, (
        f"funduq's own dependencies include {offenders}. An SDK listed here puts every "
        "provider's code one import away from core; the two sides are "
        "contract-coupled, not code-coupled."
    )
