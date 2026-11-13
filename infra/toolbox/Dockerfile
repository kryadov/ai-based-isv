# swarm-lab toolbox: the Linux toolchain the bootstrapper and the acceptance
# flows need, for Windows hosts where native tooling is not an option
# (e.g. Smart App Control blocks unsigned Python builds and wheel DLLs).
# Run it through infra/scripts/toolbox.sh — host network, the host docker
# socket, and the repo mounted at the SAME path the docker daemon sees, so
# nested `docker run -v <repo path>` calls from the flows resolve.
FROM docker:28-cli AS dockercli

FROM ghcr.io/astral-sh/uv:0.13.8 AS uv

FROM debian:bookworm-slim

ARG KUBECTL_VERSION=v1.37.2
ARG HELM_VERSION=v3.19.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      bash ca-certificates curl git gnupg iproute2 jq openssl \
      postgresql-client procps python3 \
 && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
 && apt-get update && apt-get install -y --no-install-recommends gh \
 && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
 && chmod +x /usr/local/bin/kubectl \
 && curl -fsSL "https://get.helm.sh/helm-${HELM_VERSION}-linux-amd64.tar.gz" \
      | tar -xz -C /usr/local/bin --strip-components=1 linux-amd64/helm

COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins
COPY --from=uv /uv /uvx /usr/local/bin/

# The repo is bind-mounted from Windows (different owner) — trust it.
RUN git config --system safe.directory '*'

COPY ollama-shim.sh /usr/local/bin/ollama

ENV UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python
