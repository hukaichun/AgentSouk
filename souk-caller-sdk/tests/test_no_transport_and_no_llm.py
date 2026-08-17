"""Two absences this package claims, made checkable.

`pyproject.toml` says the empty dependency list is the point, and
`__init__.py` says that is what makes the boundary checkable rather than a
matter of discipline. Neither sentence is true unless something reads them,
so this does.

A hard constraint, like souk's own `test_core_is_network_free.py`, and with
the same rule: if it fails, the fix is almost never to widen the list.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PACKAGE = _ROOT / "souk_caller_sdk"

# A network binding is downstream's to build. If one of these ever appears
# here, the boundary was not real.
_TRANSPORTS = {"httpx", "httpx_sse", "websockets", "aiohttp", "requests", "grpc", "fastapi"}

# Choosing how you call your own model is the one decision Keep Your Own Key
# exists to leave the caller. A default that ships inside this package is that
# decision made for them.
_LLM_CLIENTS = {"litellm", "openai", "anthropic", "google", "cohere", "mistralai", "ollama"}

# The other boundary: this package states the caller's side of the agreement
# independently, so a copy derived from souk would agree with souk by
# construction and check nothing.
_FORBIDDEN = _TRANSPORTS | _LLM_CLIENTS | {"souk", "souk_server"}


def test_the_dependency_list_is_empty():
    manifest = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert manifest["project"]["dependencies"] == []


def test_nothing_here_imports_a_transport_an_llm_client_or_souk():
    offenders = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                if root in _FORBIDDEN:
                    offenders.append(f"{path.relative_to(_ROOT)}: {root}")

    assert offenders == []
