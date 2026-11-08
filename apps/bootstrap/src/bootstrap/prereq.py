"""Prerequisites detection for the organization bootstrapper.

Проверяет наличие инструментов L0-стенда и возвращает структурированный
отчёт (ok/missing + путь/версия). Никаких side-effect'ов.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Final

CHECKS: Final[tuple[str, ...]] = ("docker", "k3d", "helm", "kubectl", "uv", "gh", "ollama")


@dataclass(frozen=True)
class CheckResult:
    """Результат проверки одного инструмента."""

    name: str
    found: bool
    detail: str

    @property
    def ok(self) -> bool:
        return self.found


def _version_of(binary: str) -> str | None:
    exe = shutil.which(binary)
    if exe is None:
        return None
    try:
        out = subprocess.run(  # noqa: S603
            [exe, "--version"], capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (out.stdout or out.stderr).strip().splitlines()
    return first[0] if first else "installed"


def check_all(tools: tuple[str, ...] = CHECKS) -> tuple[CheckResult, ...]:
    """Проверить все инструменты; вернуть результаты в порядке `tools`."""
    results: list[CheckResult] = []
    for name in tools:
        version = _version_of(name)
        found = version is not None
        results.append(CheckResult(name=name, found=found, detail=version or "NOT FOUND"))
    return tuple(results)
