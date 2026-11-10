"""Bootstrap CLI: questionnaire, L0 section apply, demo-repo seeding."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from bootstrap import __version__
from bootstrap.gitea import README_CONTENT, REPO_NAME, GiteaClient
from bootstrap.prereq import check_all
from bootstrap.profile import Profile, save
from bootstrap.registry import ProjectRecord, offboard_project, register_or_update

app = typer.Typer(help="AI-Organisation bootstrapper", no_args_is_help=True)

REPO_ROOT = Path(__file__).resolve().parents[4]
PROFILE_PATH = REPO_ROOT / "infra" / "env.local.yaml"


def _gitea_token() -> str:
    """Gitea admin token from OpenBao (secret gitea/admin, field apitoken)."""
    pod = subprocess.run(  # noqa: S603
        [
            "bash",
            "-c",
            (
                "kubectl -n platform get pod -l app.kubernetes.io/name=openbao "
                "-o jsonpath='{.items[0].metadata.name}'"
            ),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    ).stdout.strip()
    out = subprocess.run(  # noqa: S603
        [
            "bash",
            "-c",
            (
                f"kubectl -n platform exec {pod} -- env BAO_ADDR=http://127.0.0.1:8200 "
                "BAO_TOKEN=$(cat infra/.vault-root-token) bao kv get "
                "-field=token secret/gitea/admin"
            ),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    token = out.stdout.strip()
    if not token:
        typer.secho("Failed to read the Gitea token from OpenBao.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    return token


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
    "agents",
)


@app.command()
def version() -> None:
    """Show the bootstrapper version."""
    typer.echo(f"org-bootstrap {__version__}")


@app.command()
def check() -> None:
    """Check prerequisites (docker, k3d, helm, kubectl, uv, gh, ollama)."""
    results = check_all()
    ok = True
    for r in results:
        mark = "[ok]" if r.ok else "[MISSING]"
        typer.echo(f"{mark:10s} {r.name:10s} {r.detail}")
        ok = ok and r.ok
    if not ok:
        typer.secho("Some tools are missing — install them and retry.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    typer.secho("All prerequisites are in place.", fg=typer.colors.GREEN)


@app.command()
def init(
    target: str = typer.Option("laptop-k3d", help="deployment target"),
    tracker: str = typer.Option(
        "deploy-openproject", help="tracker: deploy-openproject | connect-existing"
    ),
    tracker_url: str = typer.Option("", help="URL of the existing tracker (connect-existing)"),
    llm: str = typer.Option("host-ollama", help="models: host-ollama | external-api"),
    demo: bool = typer.Option(True, help="seed the isv-product demo repo"),
    force: bool = typer.Option(False, help="overwrite an existing profile"),
) -> None:
    """Questionnaire → profile infra/env.local.yaml (no secrets)."""
    if PROFILE_PATH.exists() and not force:
        typer.secho(
            "Profile already exists — use --force to overwrite.",
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
    typer.secho(f"Profile saved: {PROFILE_PATH}", fg=typer.colors.GREEN)
    typer.echo("Next: org-bootstrap apply")


@app.command()
def apply(
    section: str = typer.Option(
        None, help="single section (vault|cnpg|gitea|...|agents); default: all"
    ),
) -> None:
    """Idempotently apply L0 sections (wrapper over infra/scripts/bootstrap.sh)."""
    script = REPO_ROOT / "infra" / "scripts" / "bootstrap.sh"
    if not script.exists():
        typer.secho(f"Not found: {script}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    target = section or "all"
    typer.secho(f"Applying section(s): {target}", fg=typer.colors.CYAN)
    result = subprocess.run(  # noqa: S603
        ["bash", str(script), target], check=False
    )
    if result.returncode != 0:
        typer.secho(f"Section {target}: error (exit {result.returncode})", fg=typer.colors.RED)
        raise typer.Exit(code=result.returncode)
    typer.secho("Done.", fg=typer.colors.GREEN)


@app.command()
def seed_demo() -> None:
    """Seed the isv-product demo repo in Gitea (idempotent)."""
    token = _gitea_token()
    client = GiteaClient(base_url="http://localhost:30080", token=token, owner="swarm-admin")
    status = client.ensure_repo(REPO_NAME)
    client.put_file(REPO_NAME, "README.md", README_CONTENT)
    url = f"{client.base_url}/{client.owner}/{REPO_NAME}"
    typer.secho(f"Demo repo: {status} → {url}", fg=typer.colors.GREEN)


@app.command()
def onboard(
    project: str = typer.Option(..., help="имя проекта (repo slug)"),
    kind: str = typer.Option("service", help="тип: product | service | tool | assistant"),
    owner: str = typer.Option("swarm-admin", help="владелец проекта"),
    reason: str = typer.Option("new project", help="зачем создается"),
) -> None:
    """Заведение нового проекта (спека §13.1): Gitea repo + запись в реестр."""
    if kind not in {"product", "service", "tool", "assistant"}:
        typer.secho("kind must be: product | service | tool | assistant", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    token = _gitea_token()
    client = GiteaClient(base_url="http://localhost:30080", token=token, owner=owner)
    status = client.ensure_repo(project)
    register_or_update(ProjectRecord(name=project, kind=kind, owner=owner, reason=reason))
    typer.secho(
        f"Project {project}: repo {status}, registered in infra/projects.yaml",
        fg=typer.colors.GREEN,
    )


@app.command()
def offboard(
    project: str = typer.Option(..., help="имя проекта из реестра"),
    reason: str = typer.Option("not needed anymore", help="причина архивации"),
) -> None:
    """Вывод проекта из эксплуатации (спека §13.6): Gitea archived + реестр."""
    token = _gitea_token()
    client = GiteaClient(base_url="http://localhost:30080", token=token, owner="swarm-admin")
    import json as _json
    import urllib.request as _req

    req = _req.Request(
        f"{client.base_url}/api/v1/repos/{client.owner}/{project}",
        data=_json.dumps({"archived": True}).encode(),
        method="PATCH",
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"},
    )
    with _req.urlopen(req) as r:
        assert r.status == 200, f"gitea archive failed: {r.status}"
    rec = offboard_project(project, reason=reason)
    typer.secho(
        f"Project {project}: archived in Gitea + registry ({rec.status_date})",
        fg=typer.colors.GREEN,
    )


if __name__ == "__main__":
    app()
