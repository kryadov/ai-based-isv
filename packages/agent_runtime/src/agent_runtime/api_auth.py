"""Service-API token for agent_api calls (M11).

The in-cluster control plane requires ``Authorization: Bearer <token>`` on its
service endpoints (stand NodePorts bind 0.0.0.0). The daemon passes the token
to workers and the automation poller as ``AGENT_RUNTIME_API_TOKEN``; host-run
flows leave it unset against their local agent_api. The header is attached per
request to agent_api calls only — shared clients also talk to Gitea,
Prometheus and LiteLLM, which must never see it.
"""

from __future__ import annotations

import os

API_TOKEN_ENV = "AGENT_RUNTIME_API_TOKEN"


def api_headers() -> dict[str, str]:
    token = os.environ.get(API_TOKEN_ENV, "")
    return {"Authorization": f"Bearer {token}"} if token else {}
