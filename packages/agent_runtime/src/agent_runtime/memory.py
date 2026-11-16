"""Project episodic memory on the worker side (M7, ADR-0003).

- :class:`MemoryClient` talks to the control plane's ``/memory`` API. It is
  FAIL-OPEN: a memory outage never fails a vertex (callers audit the skip).
- :func:`extract_lessons` is the light **memory-extractor** role: the task, the
  changed files and the review summary → at most two lessons (JSON), via the
  light LiteLLM alias. Anything unparseable yields no lessons.
- :func:`memory_prompt_section` renders a memory slice for the agent prompt.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from agent_runtime.api_auth import api_headers

logger = logging.getLogger(__name__)

MAX_LESSONS = 2
PROMPT_BODY_CHARS = 400
PROMPT_HEADER = "Project memory (lessons from earlier tasks — follow them):"
_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

_EXTRACTOR_SYSTEM = (
    "You are the memory-extractor of a software delivery platform. From a finished "
    "task, write at most two short, reusable lessons for future tasks on the same "
    "project: concrete conventions, pitfalls or decisions — not a summary of the "
    'change. Answer ONLY with JSON: {"lessons": [{"title": "...", "body": "..."}]}. '
    'If there is nothing reusable, answer {"lessons": []}.'
)


class MemoryClient:
    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")

    async def add(
        self,
        project: str,
        kind: str,
        title: str,
        body: str,
        paths: list[str],
        *,
        run_id: int | None = None,
        vertex_id: int | None = None,
        tags: list[str] | None = None,
    ) -> int | None:
        payload = {
            "project": project,
            "kind": kind,
            "title": title,
            "body": body,
            "paths": paths,
            "tags": tags or [],
            "run_id": run_id,
            "vertex_id": vertex_id,
        }
        try:
            response = await self._http.post(
                f"{self._base}/memory/episodes", json=payload, headers=api_headers()
            )
            response.raise_for_status()
            return int(response.json()["id"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("memory add skipped (%s): %s", kind, exc)
            return None

    async def slice(
        self, project: str, paths: list[str], text: str, limit: int = 5
    ) -> list[dict[str, Any]]:
        params = {"project": project, "paths": ",".join(paths), "q": text, "limit": str(limit)}
        try:
            response = await self._http.get(
                f"{self._base}/memory/slice", params=params, headers=api_headers()
            )
            response.raise_for_status()
            episodes: list[dict[str, Any]] = response.json().get("episodes", [])
            return episodes
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("memory slice skipped: %s", exc)
            return []


def parse_lessons(text: str) -> list[dict[str, str]]:
    """Lessons from a model answer: bare or fenced JSON; anything else → []."""
    candidates = [m.group(1) for m in _FENCE_RE.finditer(text)] + [text.strip()]
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        items = data.get("lessons") if isinstance(data, dict) else None
        if not isinstance(items, list):
            continue
        lessons = [
            {"title": str(i["title"]).strip(), "body": str(i.get("body", "")).strip()}
            for i in items
            if isinstance(i, dict) and str(i.get("title", "")).strip()
        ]
        return lessons[:MAX_LESSONS]
    return []


async def extract_lessons(
    http: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    model: str,
    task: str,
    changed: list[str],
    review_summary: str,
) -> list[dict[str, str]]:
    user = (
        f"Task: {task}\nChanged files: {', '.join(changed) or '(none)'}\n"
        f"Review summary: {review_summary or '(none)'}"
    )
    try:
        response = await http.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": _EXTRACTOR_SYSTEM},
                    {"role": "user", "content": user},
                ],
                "max_tokens": 600,
                "temperature": 0.2,
            },
            timeout=120.0,
        )
        response.raise_for_status()
        content = str(response.json()["choices"][0]["message"]["content"] or "")
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.warning("memory-extractor skipped: %s", exc)
        return []
    return parse_lessons(content)


def memory_prompt_section(episodes: list[dict[str, Any]]) -> str:
    if not episodes:
        return ""
    lines = [PROMPT_HEADER]
    for episode in episodes:
        body = " ".join(str(episode.get("body", "")).split())[:PROMPT_BODY_CHARS]
        lines.append(f"- [{episode.get('kind')}] {episode.get('title')}: {body}")
    return "\n".join(lines)


def project_of(task_ref: str, repo_url: str) -> str:
    """``owner/repo`` of the task: from ``owner/repo#N`` or the repo URL."""
    if task_ref and "#" in task_ref:
        return task_ref.split("#", 1)[0]
    segments = [s for s in urlsplit(repo_url).path.split("/") if s]
    if len(segments) >= 2:
        return f"{segments[-2]}/{segments[-1].removesuffix('.git')}"
    return repo_url
