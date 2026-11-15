"""The isv-product seed (spec §6, M6): one standalone repo tree.

The template lives in the monorepo (``apps/isv_product`` + the service deploy
templates + the python check profile); the seeded copy is canonical in Gitea
(``swarm-lab/isv-product``). The monorepo ``pyproject.toml`` relies on
workspace-level dev tools, so the seed gets its own standalone one.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Final

from bootstrap.profiles import profile_files

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[4]
APP_DIR: Final[Path] = REPO_ROOT / "apps" / "isv_product"
DEPLOY_DIR: Final[Path] = REPO_ROOT / "infra" / "templates" / "deploy" / "service"

PYPROJECT: Final[str] = """[project]
name = "isv_product"
version = "0.1.0"
description = "Demo service seeded by the organization bootstrapper"
requires-python = ">=3.12"
dependencies = ["fastapi>=0.115", "uvicorn>=0.34", "prometheus-client>=0.21"]

[dependency-groups]
dev = ["pytest>=8", "httpx>=0.27"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/isv_product"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[tool.mypy]
mypy_path = "src"
explicit_package_bases = true
strict = true
exclude = ["deploy/"]

[tool.ruff]
target-version = "py312"
line-length = 100
src = ["src"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM"]
"""

README: Final[str] = """# isv-product

Organization demo service (seeded by the bootstrapper).

- `src/isv_product` — FastAPI service: `/api/greet`, `/healthz`, `/metrics`.
- `deploy/` — Argo Rollouts canary + analysis + k6 traffic (human-only: agents
  cannot change `deploy/**`).
- Changes arrive as tracker tasks through the platform corridor; every merge
  is deployed to `prod` as a canary by the platform deployer.
"""


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n == "__pycache__" or n.endswith(".pyc")}


def seed_tree(dest: Path) -> list[str]:
    """Assemble the isv-product repo into ``dest``; return its sorted relative paths."""
    dest.mkdir(parents=True, exist_ok=True)
    for sub in ("src", "tests"):
        # copyfile, not copy2: no mode bits — files from a Windows mount look
        # executable (0777), which ruff (EXE002) and git would record.
        shutil.copytree(
            APP_DIR / sub,
            dest / sub,
            ignore=_ignore,
            dirs_exist_ok=True,
            copy_function=shutil.copyfile,
        )
    shutil.copyfile(APP_DIR / "Dockerfile", dest / "Dockerfile")
    (dest / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (dest / "README.md").write_text(README, encoding="utf-8")
    shutil.copytree(DEPLOY_DIR, dest / "deploy", dirs_exist_ok=True, copy_function=shutil.copyfile)
    for path, content in profile_files("python").items():
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return sorted(
        p.relative_to(dest).as_posix()
        for p in dest.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(dest).parts
    )


def push_seed(
    repo_url: str,
    token: str,
    workdir: Path,
    *,
    message: str,
    build: Callable[[Path], list[str]] | None = None,
) -> None:
    """Clone ``repo_url`` (auto-initialised main), lay the seed over it, push main.

    The token travels as an http.extraHeader in ``GIT_CONFIG_*`` env — never in
    the URL or argv. Runs before branch protection is applied.
    """
    import os
    import subprocess

    env = {
        **os.environ,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: token {token}",
        "GIT_AUTHOR_NAME": "org-bootstrap",
        "GIT_AUTHOR_EMAIL": "bootstrap@lab.local",
        "GIT_COMMITTER_NAME": "org-bootstrap",
        "GIT_COMMITTER_EMAIL": "bootstrap@lab.local",
    }

    def git(*args: str, cwd: Path | None = None) -> None:
        subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)  # noqa: S603, S607

    clone = workdir / "repo"
    git("clone", "-q", repo_url, str(clone))
    (build or seed_tree)(clone)
    git("add", "-A", cwd=clone)
    git("commit", "-qm", message, cwd=clone)
    git("push", "-q", "origin", "HEAD:main", cwd=clone)
