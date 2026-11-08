"""Bootstrap CLI: вопросник, применение секций L0, засев demo-репо."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from bootstrap import __version__
from bootstrap.prereq import check_all
from bootstrap.profile import Profile, save

app = typer.Typer(help="AI-Organisation bootstrapper", no_args_is_help=True)

REPO_ROOT = Path(__file__).resolve().parents[4]
PROFILE_PATH = REPO_ROOT / "infra" / "env.local.yaml"
SECTIONS = (
    "vault",
    "cnpg",
    "gitea",
    "runner",
    "argocd",
    "monitoring",
    "loki",
    "demo",
    "openproject",
    "litellm",
)


@app.command()
def version() -> None:
    """Показать версию бутстреппера."""
    typer.echo(f"org-bootstrap {__version__}")


@app.command()
def check() -> None:
    """Проверить prerequisites (docker, k3d, helm, kubectl, uv, gh, ollama)."""
    results = check_all()
    ok = True
    for r in results:
        mark = "[ok]" if r.ok else "[MISSING]"
        typer.echo(f"{mark:10s} {r.name:10s} {r.detail}")
        ok = ok and r.ok
    if not ok:
        typer.secho("Часть инструментов отсутствует — установи и повтори.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho("Все prerequisites на месте.", fg=typer.colors.GREEN)


@app.command()
def init(
    target: str = typer.Option("laptop-k3d", help="цель развёртывания"),
    tracker: str = typer.Option(
        "deploy-openproject", help="трекер: deploy-openproject | connect-existing"
    ),
    tracker_url: str = typer.Option("", help="URL существующего трекера (connect-existing)"),
    llm: str = typer.Option("host-ollama", help="модели: host-ollama | external-api"),
    demo: bool = typer.Option(True, help="засеять demo-репо isv-product"),
    force: bool = typer.Option(False, help="перезаписать существующий профиль"),
) -> None:
    """Вопросник → профиль infra/env.local.yaml (без секретов)."""
    if PROFILE_PATH.exists() and not force:
        typer.secho(
            "Профиль уже существует — используй --force для перезаписи.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)
    profile = Profile(
        target=target,
        tracker=tracker,
        tracker_existing_url=tracker_url,
        llm=llm,
        demo=demo,
    )
    save(profile, PROFILE_PATH)
    typer.secho(f"Профиль сохранён: {PROFILE_PATH}", fg=typer.colors.GREEN)
    typer.echo("Далее: org-bootstrap apply")


@app.command()
def apply(
    section: str = typer.Option(None, help="одна секция (vault|cnpg|gitea|...); по умолчанию все"),
) -> None:
    """Идемпотентно применить секции L0 (обёртка над infra/scripts/bootstrap.sh)."""
    script = REPO_ROOT / "infra" / "scripts" / "bootstrap.sh"
    if not script.exists():
        typer.secho(f"Не найден {script}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    target = section or "all"
    typer.secho(f"Применяю секцию(и): {target}", fg=typer.colors.CYAN)
    result = subprocess.run(  # noqa: S603
        ["bash", str(script), target], check=False
    )
    if result.returncode != 0:
        typer.secho(f"Секция {target}: ошибка (exit {result.returncode})", fg=typer.colors.RED)
        raise typer.Exit(code=result.returncode)
    typer.secho("Готово.", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
