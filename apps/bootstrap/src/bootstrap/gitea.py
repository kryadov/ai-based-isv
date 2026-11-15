"""Gitea API client for demo-repo seeding (stdlib urllib; token from OpenBao)."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request

REPO_NAME = "isv-product"
README_CONTENT = """# isv-product

Organization demo project (seeded by the bootstrapper).
Real code appears via tracker tasks through the platform pipeline.
"""


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
        """Create a repo with auto-init; True if created, False if it already exists."""
        code, _ = self._request(
            "POST",
            "/user/repos",
            {"name": repo, "private": private, "auto_init": True},
        )
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
        """Create/update a file in the repo (contents API, base64)."""
        b64 = base64.b64encode(content.encode()).decode()
        sha = ""
        code, body = self._request("GET", f"/repos/{self.owner}/{repo}/contents/{path}")
        if code == 200:
            sha = str(body.get("sha", ""))
        payload: dict[str, object] = {
            "message": f"bootstrap: seed {path}",
            "content": b64,
        }
        if sha:
            payload["sha"] = sha
        method = "PUT" if sha else "POST"
        self._request(method, f"/repos/{self.owner}/{repo}/contents/{path}", payload)

    def apply_check_profile(self, repo: str, *, stack: str, git_url: str) -> None:
        """Seed the stack's CI corridor + merge policy and protect ``main`` (M5).

        Order matters: files first, then the PLATFORM_GIT_URL variable (in-cluster
        Gitea for CI checkout), then branch protection — afterwards ``main``
        accepts no direct commits and merges only with every CI context green.
        """
        from bootstrap.profiles import REQUIRED_CONTEXTS, profile_files

        for path, content in profile_files(stack).items():
            self.put_file(repo, path, content)
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
        base = f"/repos/{self.owner}/{repo}/branch_protections"
        code, _ = self._request("POST", base, protection)
        if code in (409, 422):  # already protected: update it
            code, _ = self._request("PATCH", f"{base}/main", protection)
        if code >= 400:
            raise RuntimeError(f"gitea: could not protect main on {repo} ({code})")
