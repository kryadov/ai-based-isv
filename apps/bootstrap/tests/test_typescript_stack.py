"""Polyglot dogfood (M9): the typescript check profile and the ts-service template."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from bootstrap.profiles import profile_files
from bootstrap.templates import TEMPLATES_DIR, template_tree


def _workflow() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(profile_files("typescript")[".gitea/workflows/ci.yml"])
    return data


def _runs(job: str) -> str:
    return "\n".join(str(s.get("run", "")) for s in _workflow()["jobs"][job]["steps"])


def test_typescript_profile_gates() -> None:
    wf = _workflow()
    assert wf["name"] == "ci" and set(wf["jobs"]) == {"checks", "security"}
    checks = _runs("checks")
    order = [checks.index(s) for s in ("npm ci", "npx tsc --noEmit", "npx eslint .", "npm test")]
    assert order == sorted(order)
    python_security = yaml.safe_load(profile_files("python")[".gitea/workflows/ci.yml"])
    assert wf["jobs"]["security"] == python_security["jobs"]["security"]


def test_ts_service_template_tree(tmp_path: Path) -> None:
    paths = template_tree("ts-service", tmp_path)
    for expected in (
        "package.json",
        "package-lock.json",
        "tsconfig.json",
        "eslint.config.mjs",
        ".gitignore",
        "src/server.ts",
        "src/greet.ts",
        "test/greet.test.ts",
    ):
        assert expected in paths, expected
    assert not [p for p in paths if p.startswith(("node_modules/", "dist/"))]
    ignore = (tmp_path / ".gitignore").read_text(encoding="utf-8").split()
    assert "node_modules/" in ignore and "dist/" in ignore


def test_ts_service_scripts_and_lock_match() -> None:
    root = TEMPLATES_DIR / "ts-service"
    pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
    assert {"build", "test", "lint"} <= set(pkg["scripts"])
    assert "node --test" in pkg["scripts"]["test"]
    lock = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["packages"][""]["devDependencies"] == pkg["devDependencies"]


def test_unknown_template_is_rejected(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError, match="unknown template"):
        template_tree("../etc", tmp_path)
