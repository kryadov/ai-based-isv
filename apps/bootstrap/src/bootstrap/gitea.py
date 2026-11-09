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
