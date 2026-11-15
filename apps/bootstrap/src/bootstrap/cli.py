"""Bootstrap CLI: questionnaire, L0 section apply, demo-repo seeding."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer

from bootstrap import __version__
from bootstrap.gitea import REPO_NAME, GiteaClient
from bootstrap.prereq import check_all
from bootstrap.profile import Profile, save
from bootstrap.registry import ProjectRecord, offboard_project, register_or_update
from bootstrap.seed import push_seed
from bootstrap.templates import TEMPLATES_DIR, template_tree

app = typer.Typer(help="AI-Organisation bootstrapper", no_args_is_help=True)
memory_app = typer.Typer(
    help="Org memory (M7): seed the repo, promote lessons", no_args_is_help=True
)
app.add_typer(memory_app, name="memory")

REPO_ROOT = Path(__file__).resolve().parents[4]
PROFILE_PATH = REPO_ROOT / "infra" / "env.local.yaml"


# In-cluster Gitea for CI job checkout (ROOT_URL is a host NodePort).
PLATFORM_GIT_URL = "http://gitea-http.platform.svc.cluster.local:3000"


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
    """Check prerequisites for the SWARM_CLUSTER flavour (k3d only for k3d)."""
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
        None, help="single section (cluster|vault|cnpg|gitea|...|agents|mcp); default: all"
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
    """Seed swarm-lab/isv-product: service code + deploy/ + check profile (idempotent)."""
    import tempfile

    token = _gitea_token()
    client = GiteaClient(base_url="http://localhost:30080", token=token, owner="swarm-lab")
    status = client.ensure_repo(REPO_NAME)
    url = f"{client.base_url}/{client.owner}/{REPO_NAME}"
    if status == "created":
        with tempfile.TemporaryDirectory() as tmp:
            push_seed(f"{url}.git", token, Path(tmp), message="seed: isv-product service + deploy")
    else:
        typer.secho("Repo exists: seed not re-applied (re-seed is explicit)", fg="yellow")
    client.apply_check_profile(REPO_NAME, stack="python", git_url=PLATFORM_GIT_URL)
    typer.secho(f"Demo repo: {status} → {url}", fg=typer.colors.GREEN)


@app.command()
def onboard(
    project: str = typer.Option(..., help="имя проекта (repo slug)"),
    kind: str = typer.Option("service", help="тип: product | service | tool | assistant"),
    owner: str = typer.Option(
        "swarm-lab", help="Gitea org (or user) that owns the repo; agents reach swarm-lab repos"
    ),
    reason: str = typer.Option("new project", help="зачем создается"),
    stack: str = typer.Option("python", help="check profile (CI corridor) for the repo"),
    push_whitelist: list[str] = typer.Option(  # noqa: B008 - typer option factory
        [], "--push-whitelist", help="users allowed to push main directly (platform repo sync)"
    ),
    template: str = typer.Option(
        "", "--template", help="seed code from infra/templates/projects/<name> (new repos)"
    ),
) -> None:
    """Заведение нового проекта (спека §13.1): Gitea repo + запись в реестр."""
    if kind not in {"product", "service", "tool", "assistant"}:
        typer.secho("kind must be: product | service | tool | assistant", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    if template and not (TEMPLATES_DIR / template).is_dir():
        typer.secho(f"unknown template {template!r}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    token = _gitea_token()
    client = GiteaClient(base_url="http://localhost:30080", token=token, owner=owner)
    status = client.ensure_repo(project)
    if template and status == "created":
        # Template code lands BEFORE the profile and branch protection (M9).
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            push_seed(
                f"{client.base_url}/{client.owner}/{project}.git",
                token,
                Path(tmp),
                message=f"seed: {template} template",
                build=lambda dest: template_tree(template, dest),
            )
    # The gate corridor (M5): CI check profile + merge policy + protected main.
    client.apply_check_profile(
        project, stack=stack, git_url=PLATFORM_GIT_URL, push_whitelist=push_whitelist
    )
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


def _fetch_episode(api: str, episode_id: int) -> dict[str, object] | None:
    """Episode from the control plane (agent_api ``/memory/episodes/{id}``)."""
    import json as _json
    import urllib.error as _err
    import urllib.request as _req

    try:
        with _req.urlopen(f"{api.rstrip('/')}/memory/episodes/{episode_id}", timeout=30) as r:  # noqa: S310
            data: dict[str, object] = _json.load(r)
            return data
    except _err.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


@memory_app.command("seed")
def memory_seed() -> None:
    """Seed swarm-lab/org-memory (lessons/, reuse catalog v1, docs profile; idempotent)."""
    import tempfile

    from bootstrap.org_memory import ORG_MEMORY_REPO, seed_tree

    token = _gitea_token()
    client = GiteaClient(base_url="http://localhost:30080", token=token, owner="swarm-lab")
    status = client.ensure_repo(ORG_MEMORY_REPO)
    url = f"{client.base_url}/{client.owner}/{ORG_MEMORY_REPO}"
    if status == "created":
        with tempfile.TemporaryDirectory() as tmp:
            push_seed(f"{url}.git", token, Path(tmp), message="seed: org memory", build=seed_tree)
    client.apply_check_profile(ORG_MEMORY_REPO, stack="docs", git_url=PLATFORM_GIT_URL)
    typer.secho(f"Org memory: {status} → {url}", fg=typer.colors.GREEN)


@memory_app.command("promote")
def memory_promote(
    episode: int = typer.Option(..., "--episode", help="episode id in project memory"),
    api: str = typer.Option("http://127.0.0.1:8472", help="control plane (agent_api) URL"),
) -> None:
    """Open a PR promoting an episode into org memory (human-reviewed, ADR-0003)."""
    from bootstrap.org_memory import ORG_MEMORY_REPO, lesson_markdown, lesson_path

    found = _fetch_episode(api, episode)
    if found is None:
        typer.secho(f"episode {episode} not found at {api}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    path = lesson_path(found)
    client = GiteaClient(base_url="http://localhost:30080", token=_gitea_token(), owner="swarm-lab")
    pr_url = client.propose_file(
        ORG_MEMORY_REPO,
        path,
        lesson_markdown(found),
        branch=f"lesson/{path.removeprefix('lessons/').removesuffix('.md')}",
        title=f"lesson: {found['title']}",
        body=(
            f"Promotion of project episode {episode} ({found['project']}, kind "
            f"{found['kind']}) into org memory. Review: is it reusable beyond this task?"
        ),
    )
    typer.secho(f"Promotion PR: {pr_url}", fg=typer.colors.GREEN)
