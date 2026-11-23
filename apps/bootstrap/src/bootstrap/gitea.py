"""Gitea API client for demo-repo seeding (stdlib urllib; token from OpenBao)."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

REPO_NAME = "isv-product"
README_CONTENT = """# isv-product

Organization demo project (seeded by the bootstrapper).
Real code appears via tracker tasks through the platform pipeline.
"""


# Issues with this label are picked up by the platform dispatcher (M11).
INTAKE_LABEL = "swarm"
# A human adds this to an issue whose run failed to get a new run (M12).
RETRY_LABEL = "swarm-retry"
# Branch of the single PR that carries profile updates into a protected main.
PROFILE_BRANCH = "platform/profile-update"


class ProtectedMainError(RuntimeError):
    """``main`` refused a direct write (branch protection); ``sha`` = current blob."""

    def __init__(self, path: str, sha: str) -> None:
        super().__init__(f"main is protected: {path}")
        self.path = path
        self.sha = sha


class GiteaClient:
    """Minimal Gitea API v1 client (only what the bootstrapper needs)."""

    def __init__(self, base_url: str, token: str, owner: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.owner = owner

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        url = f"{self.base_url}/api/v1{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"token {self.token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req) as r:
                code = r.status
                raw = r.read()
        except urllib.error.HTTPError as e:
            code = e.code
            raw = e.read()
        body = json.loads(raw) if raw else {}
        return code, body

    def repo_exists(self, repo: str) -> bool:
        code, _ = self._request("GET", f"/repos/{self.owner}/{repo}")
        return code == 200

    def create_repo(self, repo: str, private: bool = True) -> bool:
        """Create a repo with auto-init under ``owner``; True if created, False if it exists.

        ``owner`` is normally an org (``swarm-lab``: the agents' team reaches
        only org repos); a 404 from the org endpoint means ``owner`` is the
        token's own user, so the repo goes to ``/user/repos``.
        """
        payload: dict[str, object] = {"name": repo, "private": private, "auto_init": True}
        code, _ = self._request("POST", f"/orgs/{self.owner}/repos", payload)
        if code == 404:
            code, _ = self._request("POST", "/user/repos", payload)
        if code == 201:
            return True
        if code == 409:  # already exists
            return False
        raise RuntimeError(f"gitea: repo create {repo} → HTTP {code}")

    def ensure_repo(self, repo: str = REPO_NAME) -> str:
        """Ensure the repo exists; return status 'created' | 'exists'."""
        if self.repo_exists(repo):
            return "exists"
        return "created" if self.create_repo(repo) else "exists"

    def put_file(self, repo: str, path: str, content: str) -> None:
        """Create/update a file in the repo (contents API, base64).

        Identical content is left alone (no empty commit), and any HTTP error
        raises — a silently missing gate file would leave the corridor open.
        """
        b64 = base64.b64encode(content.encode()).decode()
        sha = ""
        code, body = self._request("GET", f"/repos/{self.owner}/{repo}/contents/{path}")
        if code == 200:
            sha = str(body.get("sha", ""))
            current = str(body.get("content", "")).replace("\n", "")
            if current and base64.b64decode(current) == content.encode():
                return
        payload: dict[str, object] = {
            "message": f"bootstrap: seed {path}",
            "content": b64,
        }
        if sha:
            payload["sha"] = sha
        method = "PUT" if sha else "POST"
        code, _ = self._request(method, f"/repos/{self.owner}/{repo}/contents/{path}", payload)
        if code == 403:
            raise ProtectedMainError(path, sha)
        if code >= 400:
            raise RuntimeError(f"gitea: could not write {path} in {repo} ({code})")

    def propose_files(
        self, repo: str, files: Mapping[str, tuple[str, str]], *, title: str, body: str
    ) -> str:
        """One PR from ``PROFILE_BRANCH`` with every ``path → (content, sha)`` (M11).

        A protected ``main`` takes gate-file updates only through the corridor;
        while that PR is open, a re-run reports it instead of opening another."""
        branch_path = f"/repos/{self.owner}/{repo}/branches/{PROFILE_BRANCH}"
        code, _ = self._request("GET", branch_path)
        if code == 200:
            return f"pending: {PROFILE_BRANCH}"
        changes: list[dict[str, str]] = []
        for path, (content, sha) in files.items():
            change = {
                "operation": "update" if sha else "create",
                "path": path,
                "content": base64.b64encode(content.encode()).decode(),
            }
            if sha:
                change["sha"] = sha
            changes.append(change)
        payload: dict[str, object] = {
            "branch": "main",
            "new_branch": PROFILE_BRANCH,
            "message": title,
            "files": changes,
        }
        code, _ = self._request("POST", f"/repos/{self.owner}/{repo}/contents", payload)
        if code >= 400:
            raise RuntimeError(f"gitea: could not commit the profile update on {repo} ({code})")
        pr: dict[str, object] = {
            "title": title,
            "head": PROFILE_BRANCH,
            "base": "main",
            "body": body,
        }
        code, data = self._request("POST", f"/repos/{self.owner}/{repo}/pulls", pr)
        if code >= 400:
            raise RuntimeError(f"gitea: could not open the profile PR on {repo} ({code})")
        return str(data.get("html_url", ""))

    def propose_file(
        self, repo: str, path: str, content: str, *, branch: str, title: str, body: str
    ) -> str:
        """Commit ``path`` on a new ``branch`` off main and open a PR; return its URL."""
        payload: dict[str, object] = {
            "message": title,
            "content": base64.b64encode(content.encode()).decode(),
            "branch": "main",
            "new_branch": branch,
        }
        code, _ = self._request("POST", f"/repos/{self.owner}/{repo}/contents/{path}", payload)
        if code >= 400:
            raise RuntimeError(f"gitea: could not commit {path} on {branch} ({code})")
        pr: dict[str, object] = {"title": title, "head": branch, "base": "main", "body": body}
        code, data = self._request("POST", f"/repos/{self.owner}/{repo}/pulls", pr)
        if code >= 400:
            raise RuntimeError(f"gitea: could not open PR {branch} → main ({code})")
        return str(data.get("html_url", ""))

    def ensure_label(self, repo: str, name: str, color: str, description: str) -> None:
        """Create the issue label unless the repo already has it (M11 intake label)."""
        code, body = self._request("GET", f"/repos/{self.owner}/{repo}/labels")
        existing: Any = body
        if (
            code == 200
            and isinstance(existing, list)
            and any(isinstance(lb, dict) and lb.get("name") == name for lb in existing)
        ):
            return
        code, _ = self._request(
            "POST",
            f"/repos/{self.owner}/{repo}/labels",
            {"name": name, "color": color, "description": description},
        )
        if code >= 400 and code != 409:
            raise RuntimeError(f"gitea: could not create label {name!r} on {repo} ({code})")

    def apply_check_profile(
        self,
        repo: str,
        *,
        stack: str,
        git_url: str,
        push_whitelist: Sequence[str] = (),
        overrides: Mapping[str, str] | None = None,
    ) -> str | None:
        """Seed the stack's CI corridor + merge policy and protect ``main`` (M5).

        Order matters: files first, then the PLATFORM_GIT_URL variable (in-cluster
        Gitea for CI checkout), then branch protection — afterwards ``main``
        accepts no direct commits and merges only with every CI context green.
        """
        from bootstrap.profiles import REQUIRED_CONTEXTS, profile_files

        # ``overrides`` replace profile files per repo (e.g. a project.yaml with
        # a deploy target) so a re-run never flips a file between two contents.
        files = {**profile_files(stack), **(overrides or {})}
        pending: dict[str, tuple[str, str]] = {}
        for path, content in files.items():
            try:
                self.put_file(repo, path, content)
            except ProtectedMainError as exc:
                pending[path] = (content, exc.sha)
        proposal = (
            self.propose_files(
                repo,
                pending,
                title="platform: update check-profile files",
                body="Gate files from the platform check profile; main is protected, "
                "so the update goes through the corridor (merge after CI).",
            )
            if pending
            else None
        )
        self.ensure_label(repo, INTAKE_LABEL, "#2f5bd3", "the platform picks this issue up (M11)")
        self.ensure_label(repo, RETRY_LABEL, "#d97706", "run a failed swarm task again (M12)")
        var_path = f"/repos/{self.owner}/{repo}/actions/variables/PLATFORM_GIT_URL"
        code, _ = self._request("POST", var_path, {"value": git_url})
        if code == 409:
            code, _ = self._request("PUT", var_path, {"value": git_url})
        if code >= 400:
            raise RuntimeError(f"gitea: could not set PLATFORM_GIT_URL on {repo} ({code})")
        protection: dict[str, object] = {
            "branch_name": "main",
            "enable_status_check": True,
            "status_check_contexts": list(REQUIRED_CONTEXTS),
        }
        if push_whitelist:
            # Only these users may push main directly (the platform repo's
            # GitHub → Gitea fast-forward); everyone else goes through PRs.
            protection.update(
                enable_push=True,
                enable_push_whitelist=True,
                push_whitelist_usernames=list(push_whitelist),
            )
        base = f"/repos/{self.owner}/{repo}/branch_protections"
        code, _ = self._request("POST", base, protection)
        if code in (403, 409, 422):  # already protected (Gitea: 403 "already exist")
            code, _ = self._request("PATCH", f"{base}/main", protection)
        if code >= 400:
            raise RuntimeError(f"gitea: could not protect main on {repo} ({code})")
        return proposal
