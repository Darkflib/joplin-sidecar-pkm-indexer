"""Source guards: httpx (network) is confined to the two network modules.

PRD §8.5 — no external network access beyond the Joplin client. Keeping httpx out
of every other module makes it structurally hard to add a stray outbound call.
"""

import ast
from pathlib import Path

import pkm_sidecar

SRC = Path(pkm_sidecar.__file__).parent
# joplin_client owns the HTTP client; security owns LocalOnlyTransport / URL guard.
HTTPX_ALLOWED = {"joplin_client.py", "security.py"}


def _imports_httpx(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            a.name.split(".")[0] == "httpx" for a in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "httpx":
            return True
    return False


def test_httpx_confined_to_network_modules() -> None:
    offenders = [
        p.name for p in SRC.glob("*.py") if _imports_httpx(p) and p.name not in HTTPX_ALLOWED
    ]
    assert offenders == [], f"unexpected httpx imports outside network modules: {offenders}"
