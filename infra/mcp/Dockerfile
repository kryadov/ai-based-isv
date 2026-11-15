# MCP servers image (task-source, llm). Built on the host (context: the package) and
# imported into k3d (`agents` is default-deny: pods cannot pip-install at boot).
#   docker build -f infra/mcp/Dockerfile -t mcp-servers:<version> packages/mcp_servers
FROM python:3.12-slim
WORKDIR /src
COPY . /src/mcp_servers
RUN pip install --no-cache-dir /src/mcp_servers \
 && useradd --uid 10001 --no-create-home mcp
USER 10001
EXPOSE 8000
