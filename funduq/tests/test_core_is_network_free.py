"""Core implements no transport: funduq's own code neither listens nor dials.

The defense forbids verbs, not nouns. Third-party packages are allowed to
carry network code in their bellies — a2a-sdk ships httpx unconditionally,
so pretending the dependency tree is network-free was fiction from the day
it was installed. And quoting a protocol's own package is the shortcut to
staying current with it (funduq once hand-wrote A2A and answered `tasks/send`
two renames after the spec moved on); the defense must not tax importing a
SDK's vocabulary. What it must catch is funduq's own modules programming
against a transport (the static check) and anything — ours or theirs —
touching a socket when the package is imported (the behavioral probe).
See docs/design-records.md.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

FUNDUQ_PACKAGE = Path(__file__).resolve().parent.parent / "funduq"

FORBIDDEN_MODULE_PREFIXES = {
    # Transport and serving frameworks: funduq's code never programs against
    # these directly. Serving lives in the funduq-server repository.
    "grpc",
    "fastapi",
    "uvicorn",
    "starlette",
    "sse_starlette",
    "httpx",
    "websockets",
    "aiohttp",
    "requests",
    "urllib3",
    # The I/O half of protocol SDKs whose vocabulary half is welcome:
    # a2a.types, a2a.utils and a2a.extensions are the protocol speaking for
    # itself; a2a.client and the serving stack are somebody dialing/listening.
    "a2a.client",
    "a2a.server.routes",
    "a2a.server.request_handlers",
    # openai is a dialing client; only its vocabulary is fair game.
    "openai",
}

ALLOWED_MODULE_PREFIXES = {
    # KYOK's wire shapes are OpenAI chat-completions; the types package
    # imports no client.
    "openai.types",
}


def _matches(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def _core_modules() -> list[Path]:
    return sorted(
        path for path in FUNDUQ_PACKAGE.rglob("*.py") if not path.name.startswith("__")
    )


def _imported_modules(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module)
    return names


@pytest.mark.parametrize("module", _core_modules(), ids=lambda p: str(p.relative_to(FUNDUQ_PACKAGE)))
def test_core_module_imports_no_transport_layer(module: Path) -> None:
    offenders = sorted(
        name
        for name in _imported_modules(module)
        if any(_matches(name, deny) for deny in FORBIDDEN_MODULE_PREFIXES)
        and not any(_matches(name, allow) for allow in ALLOWED_MODULE_PREFIXES)
    )
    assert not offenders, (
        f"{module.name} imports {offenders}, which is a transport or a protocol "
        "SDK's I/O layer. funduq's own code neither listens nor dials — move "
        "whatever needs this into the serving layer, or import the SDK's "
        "vocabulary modules instead."
    )


def test_generated_wire_stubs_are_not_reachable_from_core() -> None:
    offenders = [m.name for m in _core_modules() if "grpc_gen" in m.read_text()]
    assert not offenders, f"core modules referencing generated worker-channel stubs: {offenders}"


_SOCKET_PROBE = """
import sys

VERBS = ("socket.connect", "socket.bind", "socket.listen", "socket.sendto",
         "socket.sendmsg", "socket.getaddrinfo", "socket.gethostbyname")
hits = []

def hook(event, args):
    if event.startswith(VERBS):
        hits.append(event)

sys.addaudithook(hook)

import importlib
import pkgutil

import funduq

for info in pkgutil.walk_packages(funduq.__path__, prefix="funduq."):
    if ".alembic" in info.name:
        continue  # migration scripts run under alembic, not by import
    importlib.import_module(info.name)
    if hits:
        print(f"{info.name}: {sorted(set(hits))}")
        sys.exit(1)
print("clean")
"""


def test_importing_the_whole_package_touches_no_socket() -> None:
    """The behavioral half of the invariant: whatever the dependency tree
    contains, importing every funduq module must perform no network verb.
    Runs in a subprocess so the probe sees every import happen fresh."""
    result = subprocess.run(
        [sys.executable, "-c", _SOCKET_PROBE],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0 and "clean" in result.stdout, (
        f"importing funduq performed network verbs:\n{result.stdout}{result.stderr}"
    )


SERVING_FRAMEWORKS = {"fastapi", "uvicorn", "starlette", "sse-starlette", "grpcio", "httpx"}


def _declared_dependencies() -> str:
    pyproject = (FUNDUQ_PACKAGE.parent / "pyproject.toml").read_text()
    return "\n".join(
        line for line in pyproject.split("[dependency-groups]")[0].splitlines()
        if not line.lstrip().startswith("#")
    )


def test_no_serving_framework_is_a_direct_dependency() -> None:
    """Direct dependencies may contain network code (a2a-sdk ships httpx);
    what funduq must never *ask for* is a framework whose only use here would
    be to serve or dial — that intent belongs to the funduq-server repo."""
    declared = _declared_dependencies()
    offenders = sorted(name for name in SERVING_FRAMEWORKS if name in declared)
    assert not offenders, (
        f"funduq's own dependencies include {offenders}. Serving belongs in the "
        "funduq-server repo; a framework listed here says this repo intends to "
        "listen or dial."
    )


def test_no_serving_extra_of_a_protocol_sdk_is_requested() -> None:
    requested = {
        extra.strip()
        for match in re.findall(r"a2a-sdk\[([^\]]*)\]", _declared_dependencies())
        for extra in match.split(",")
    }
    offenders = sorted(requested & {"http-server", "grpc", "fastapi", "all"})
    assert not offenders, (
        f"a2a-sdk is requested with serving extras {offenders}; funduq wants the "
        "protocol's vocabulary, not its server."
    )
