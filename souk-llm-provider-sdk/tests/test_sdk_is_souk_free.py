from __future__ import annotations

import ast
from pathlib import Path

import pytest

SDK_PACKAGE = Path(__file__).resolve().parent.parent / "souk_llm_provider_sdk"

FORBIDDEN_ROOTS = {"souk"}


def _sdk_modules() -> list[Path]:
    return sorted(SDK_PACKAGE.rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("module", _sdk_modules(), ids=lambda p: str(p.relative_to(SDK_PACKAGE)))
def test_sdk_module_does_not_import_souk(module: Path) -> None:
    offenders = _imported_roots(module) & FORBIDDEN_ROOTS
    assert not offenders, (
        f"{module.name} imports {sorted(offenders)}. The SDK models an LLM provider "
        "that runs in a different process with no access to souk's code; it may share "
        "only the wire contract, which each side computes independently so souk's "
        "suite stays a second opinion on the bytes. Re-state the contract here "
        "instead of importing it."
    )


def test_souk_is_not_installable_as_a_dependency() -> None:
    pyproject = (SDK_PACKAGE.parent / "pyproject.toml").read_text()
    declared = "\n".join(
        line
        for line in pyproject.splitlines()
        if not line.lstrip().startswith("#") and "sdk" not in line
    )
    assert '"souk' not in declared and "'souk" not in declared, (
        "souk-llm-provider-sdk's dependencies include souk, which puts core one "
        "import away from every LLM provider. The two are contract-coupled, not "
        "code-coupled."
    )
