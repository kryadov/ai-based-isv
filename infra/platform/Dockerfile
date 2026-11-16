# Control-plane image (M11): agent_api + the panel, built by
# `infra/scripts/platform.sh up` from the pinned worktree as swarm-platform:<sha7>.
# k3s on Rancher Desktop shares the moby image store — no registry push.
FROM ghcr.io/astral-sh/uv:0.13.8 AS uv

FROM python:3.12.8-slim
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY apps ./apps
RUN uv sync --frozen --no-dev --no-editable --package agent_api \
 && useradd --create-home --uid 10001 app
COPY infra/automations ./infra/automations
COPY infra/projects.yaml ./infra/projects.yaml
ENV PATH=/app/.venv/bin:$PATH \
    AGENT_API_AUTOMATIONS_DIR=/app/infra/automations \
    AGENT_API_PROJECTS_FILE=/app/infra/projects.yaml
USER 10001
EXPOSE 8000
CMD ["uvicorn", "--factory", "agent_api.main:create_app", "--host", "0.0.0.0", "--port", "8000"]
