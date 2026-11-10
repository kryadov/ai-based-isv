#!/usr/bin/env python3
"""TaskSource poller v0 (M2c): Gitea issues → платформенный endpoint.

Полный MCP task-source реализуется в M4; этот поллер — приёмочный механизм M2c
(«изменения задач доходят до платформы») и прототип дедупликации по checkpoint.
Env: GITEA_URL, GITEA_TOKEN, OWNER, REPO, ENDPOINT, CHECKPOINT (опц.).
"""

import json
import os
import urllib.parse
import urllib.request

GITEA = os.environ["GITEA_URL"]
TOKEN = os.environ["GITEA_TOKEN"]
OWNER = os.environ["OWNER"]
REPO = os.environ["REPO"]
ENDPOINT = os.environ["ENDPOINT"]
CHECKPOINT = os.environ.get("CHECKPOINT", "/tmp/m2c-checkpoint")


def api(path, params=None):
    url = f"{GITEA}/api/v1{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"token {TOKEN}"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def deliver(items):
    payload = {
        "source": "gitea",
        "repo": f"{OWNER}/{REPO}",
        "issues": [
            {
                "number": i["number"],
                "title": i["title"],
                "state": i["state"],
                "updated_at": i["updated_at"],
            }
            for i in items
        ],
    }
    req = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        assert r.status == 200, f"endpoint вернул {r.status}"


checkpoint = ""
if os.path.exists(CHECKPOINT):
    with open(CHECKPOINT) as f:
        checkpoint = f.read().strip()

params = {"state": "all", "type": "issues"}
if checkpoint:
    params["since"] = checkpoint
issues = [i for i in api(f"/repos/{OWNER}/{REPO}/issues", params) if not i.get("pull_request")]
# Gitea `since` включающий (>=): отсекаем уже доставленное на клиенте
issues = [i for i in issues if not checkpoint or i["updated_at"] > checkpoint]

if issues:
    deliver(issues)
    newest = max(i["updated_at"] for i in issues)
    with open(CHECKPOINT, "w") as f:
        f.write(newest)
    print(f"delivered {len(issues)} issue(s)")
else:
    print("delivered 0 issues (no changes since checkpoint)")
