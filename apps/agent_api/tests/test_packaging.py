"""agent_api runs alone in its image (M11): every third-party import is declared.

In the uv workspace the dev venv has every member's dependencies, so a missing
declaration only shows up in the control-plane image (pyyaml did, live)."""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
# import name → distribution name, where they differ
DIST = {"yaml": "pyyaml", "pydantic_settings": "pydantic-settings"}


def _declared() -> set[str]:
    deps = tomllib.loads((APP / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    names = set()
    for dep in deps:
        name = dep.split(">")[0].split("=")[0].split("[")[0].split("<")[0].strip().lower()
        names.add(name.replace("_", "-"))
    return names


def _imports() -> set[str]:
    found: set[str] = set()
    for path in (APP / "src" / "agent_api").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                found.add(node.module.split(".")[0])
    return {m for m in found if m not in sys.stdlib_module_names and m != "agent_api"}


def test_every_third_party_import_is_declared() -> None:
    declared = _declared()
    implied = {"starlette", "pydantic"}  # fastapi's own dependencies
    missing = {
        m
        for m in _imports()
        if DIST.get(m, m).replace("_", "-") not in declared and m not in implied
    }
    assert not missing, f"agent_api imports undeclared packages: {sorted(missing)}"
