#!/usr/bin/env bash
# Секции L0; вызываются верхнеуровневым infra/bootstrap.sh (все секции идемпотентны).
# Порядок полного прогона: vault → cnpg → gitea (внешняя БД, если CNPG поднят) → runner.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NS=platform

# CLI-хелперы OpenBao через exec в под (под выбирается по лейблу — имя меняется с чартом).
# Рут-токен: infra/.vault-root-token (gitignored, создаётся section_vault при инициализации).
vcli() {
  local POD RTOK
  POD="$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=openbao -o jsonpath='{.items[0].metadata.name}')"
  RTOK="$(cat "$ROOT/.vault-root-token" 2>/dev/null || true)"
  [ -n "$RTOK" ] || { echo "openbao: нет root-токена ($ROOT/.vault-root-token)"; return 2; }
  kubectl -n "$NS" exec "$POD" -- env BAO_ADDR=http://127.0.0.1:8200 VAULT_ADDR=http://127.0.0.1:8200 BAO_TOKEN="$RTOK" VAULT_TOKEN="$RTOK" bao "$@"
}

section_vault() {
  # OpenBao (opensource-форк Vault, API-совместим; BSL Vault избегаем принципиально).
  # File-хранение на PVC: секреты переживают рестарты; unseal — ключи из infra/.vault-unseal-keys.json.
  helm repo add openbao https://openbao.github.io/openbao-helm >/dev/null 2>&1 || true
  helm repo update openbao >/dev/null 2>&1 || true
  helm upgrade --install vault openbao/openbao \
    --namespace "$NS" --create-namespace \
    -f "$ROOT/vault/values.yaml" --wait --timeout 5m || true
  kubectl -n "$NS" wait --for=condition=Initialized pod -l app.kubernetes.io/name=openbao --timeout=300s 2>/dev/null || true
  sleep 5
  POD="$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=openbao -o jsonpath='{.items[0].metadata.name}')"
  local OK="" STATUS_OUT
  for i in $(seq 1 30); do
    STATUS_OUT="$(kubectl -n "$NS" exec "$POD" -- env BAO_ADDR=http://127.0.0.1:8200 bao status 2>/dev/null || true)"
    if echo "$STATUS_OUT" | grep -q "Seal Type"; then OK=1; break; fi
    sleep 3
  done
  [ -n "$OK" ] || { echo "vault: pod не отвечает (30 попыток exec)"; exit 1; }
  vrun() { kubectl -n "$NS" exec "$POD" -- env BAO_ADDR=http://127.0.0.1:8200 VAULT_ADDR=http://127.0.0.1:8200 bao "$@"; }
  local keys_file="$ROOT/.vault-unseal-keys.json" init_status ukey
  sleep 3
  STATUS_OUT="$(vrun status || true)"
  if echo "$STATUS_OUT" | grep "Initialized" | grep -q "false"; then
    vrun operator init -key-shares=1 -key-threshold=1 -format=json > /tmp/vault-init.json
    mkdir -p "$(dirname "$keys_file")"
    cp /tmp/vault-init.json "$keys_file" && chmod 600 "$keys_file"
    python3 -c 'import json;print(json.load(open("'"$keys_file"'"))["root_token"])' > "$ROOT/.vault-root-token"
    chmod 600 "$ROOT/.vault-root-token"
    echo "vault: initialized (unseal keys → $keys_file, root token → .vault-root-token)"
  fi
  if echo "$STATUS_OUT" | grep "Sealed" | grep -q "true"; then
    ukey="$(python3 -c 'import json;print(json.load(open("'"$keys_file"'"))["unseal_keys_b64"][0])' 2>/dev/null || true)"
    [ -n "$ukey" ] || { echo "vault: нет unseal-ключей ($keys_file)"; exit 1; }
    vrun operator unseal "$ukey" >/dev/null
  fi
  echo "$STATUS_OUT" | grep -E "Initialized|Sealed"
  # OpenBao 2.x не монтирует secret/ автоматически — включаем kv-v2 (идемпотентно)
  vcli secrets enable -path=secret kv-v2 2>/dev/null || true
  echo "vault: kv-v2 at secret/ ready"
  vrun status | grep -E "Sealed|Initialized"
}

section_cnpg() {
  helm repo add cnpg https://cloudnative-pg.github.io/charts >/dev/null 2>&1 || true
  helm repo update cnpg >/dev/null
  helm upgrade --install cnpg cnpg/cloudnative-pg \
    --namespace cnpg-system --create-namespace --wait --timeout 5m
  kubectl apply -f "$ROOT/postgres/cluster.yaml"
  kubectl -n "$NS" wait --for=condition=Ready cluster/postgres-platform --timeout=600s
  # Дополнительные БД платформы (OpenProject, LiteLLM, control plane)
  kubectl apply -f "$ROOT/postgres/openproject-database.yaml"
  kubectl apply -f "$ROOT/postgres/litellm-database.yaml"
  kubectl apply -f "$ROOT/postgres/nodeport.yaml"
  echo "cnpg: postgres-platform Ready (pgvector standard image)"
}

# Гарантирует валидный админ-токен в Vault: проверяет текущий, при 401 создаёт новый через basic-auth.
ensure_admin_token() {
  local pw old tok code
  pw="$(vcli kv get -field=password secret/gitea/admin 2>/dev/null || true)"
  old="$(vcli kv get -field=token secret/gitea/admin 2>/dev/null || true)"
  if [ -n "$old" ]; then
    code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: token $old" http://localhost:30080/api/v1/user || true)"
    [ "$code" = "200" ] && { echo "gitea: existing admin token valid"; return 0; }
  fi
  local name="bootstrap-$(date +%s)"
  code="$(curl -s -o /tmp/gitea-token.json -w '%{http_code}' \
    -u "swarm-admin:$pw" -X POST \
    -H 'Content-Type: application/json' \
    -d "{\"name\":\"$name\",\"scopes\":[\"all\"]}" \
    http://localhost:30080/api/v1/users/swarm-admin/tokens)"
  [ "$code" = "201" ] || { echo "gitea: token create failed ($code): $(cat /tmp/gitea-token.json)"; exit 1; }
  tok="$(python3 -c 'import json;print(json.load(open("/tmp/gitea-token.json"))["sha1"])')"
  [ -n "$tok" ] || { echo "gitea: empty token sha1"; exit 1; }
  vcli kv put secret/gitea/admin username=swarm-admin "password=$pw" "token=$tok" >/dev/null
  echo "gitea: admin token stored in vault"
}

section_gitea() {
  helm repo add gitea https://dl.gitea.com/charts/ >/dev/null 2>&1 || true
  helm repo update gitea >/dev/null
  local pw dbpw
  pw="$(vcli kv get -field=password secret/gitea/admin 2>/dev/null || true)"
  [ -n "$pw" ] || { pw="$(openssl rand -hex 16)"; vcli kv put secret/gitea/admin username=swarm-admin "password=$pw" >/dev/null; }
  local extvals=()
  if kubectl -n "$NS" get secret postgres-platform-app >/dev/null 2>&1; then
    dbpw="$(kubectl -n "$NS" get secret postgres-platform-app -o jsonpath='{.data.password}' | base64 -d)"
    extvals=(--values "$ROOT/gitea/values-external-db.yaml" --set "gitea.config.database.PASSWD=$dbpw")
  fi
  helm upgrade --install gitea gitea/gitea \
    --namespace "$NS" \
    --set gitea.admin.username=swarm-admin \
    --set "gitea.admin.password=$pw" \
    --values "$ROOT/gitea/values.yaml" \
    "${extvals[@]}" --wait --timeout 10m
  kubectl -n "$NS" wait --for=condition=Ready pod -l app.kubernetes.io/name=gitea --timeout=300s 2>/dev/null || true
  ensure_admin_token
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:30080/api/v1/version || true)"
  [ "$code" = "200" ] || { echo "gitea: api not reachable ($code)"; exit 1; }
}

section_runner() {
  # Регистрационный токен runner'а из Gitea admin API → secret → apply.
  local rtok n
  ensure_admin_token
  local atok; atok="$(vcli kv get -field=token secret/gitea/admin)"
  rtok="$(curl -sf -X POST -H "Authorization: token $atok" http://localhost:30080/api/v1/admin/actions/runners/registration-token | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')"
  kubectl -n "$NS" create secret generic act-runner-registration \
    --from-literal="token=$rtok" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl apply -f "$ROOT/gitea/act-runner.yaml"
  kubectl -n "$NS" wait --for=condition=Available deployment/act-runner --timeout=300s
  n="$(curl -sf -H "Authorization: token $atok" http://localhost:30080/api/v1/admin/actions/runners | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')"
  [ "$n" -ge 1 ] || { echo "runner: not registered"; exit 1; }
  echo "runner: registered ($n)"
}

section_argocd() {
  helm repo add argo https://argoproj.github.io/argo-helm >/dev/null 2>&1 || true
  helm repo update argo >/dev/null
  helm upgrade --install argocd argo/argo-cd \
    --version 10.15.0 \
    --namespace "$NS" \
    -f "$ROOT/argocd/values.yaml" --wait --timeout 10m
  kubectl -n "$NS" wait --for=condition=Available deployment/argocd-server --timeout=300s
  # Приватный GitHub-репо требует креды для Argo CD; публичный клонируется анонимно.
  if [ "$(curl -s -o /dev/null -w '%{http_code}' 'https://github.com/kryadov/ai-based-isv/info/refs?service=git-upload-pack')" != "200" ]; then
    local gtok
    gtok="${GITHUB_TOKEN:-$(gh auth token 2>/dev/null || true)}"
    [ -n "$gtok" ] || { echo "argocd: репо приватное и нет gh-токена (gh auth login или GITHUB_TOKEN)"; exit 1; }
    kubectl -n "$NS" create secret generic argocd-repo-github-ai-based-isv \
      --from-literal=url=https://github.com/kryadov/ai-based-isv \
      --from-literal=username=kryadov \
      --from-literal="password=$gtok" \
      --type=Opaque \
      --dry-run=client -o yaml \
    | kubectl label --local -f - argocd.argoproj.io/secret-type=repository --dry-run=client -o yaml \
    | kubectl apply -f - >/dev/null
    echo "argocd: repo creds wired (private repo)"
  else
    echo "argocd: repo public — anonymous clone"
  fi
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:30081/api/version || true)"
  [ "$code" = "200" ] || { echo "argocd: api not reachable ($code)"; exit 1; }
  echo "argocd: ready (ui http://localhost:30081, repo creds wired)"
}

section_monitoring() {
  helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update prometheus-community >/dev/null
  local pw
  pw="$(vcli kv get -field=password secret/grafana/admin 2>/dev/null || true)"
  [ -n "$pw" ] || { pw="$(openssl rand -hex 12)"; vcli kv put secret/grafana/admin username=admin "password=$pw" >/dev/null; }
  kubectl -n "$NS" create secret generic grafana-admin \
    --from-literal=username=admin --from-literal="password=$pw" \
    --dry-run=client -o yaml | kubectl apply -f -
  helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
    --version 93.0.0 \
    --namespace "$NS" \
    -f "$ROOT/monitoring/values.yaml" --wait --timeout 15m
  kubectl -n "$NS" wait --for=condition=Available deployment/kube-prometheus-stack-grafana --timeout=300s 2>/dev/null || true
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' -u "admin:$pw" http://localhost:30082/api/health || true)"
  [ "$code" = "200" ] || { echo "grafana: not reachable ($code)"; exit 1; }
  echo "monitoring: kube-prometheus-stack ready (grafana http://localhost:30082)"
}

section_loki() {
  helm repo add grafana https://grafana.github.io/helm-charts >/dev/null 2>&1 || true
  helm repo update grafana >/dev/null
  helm upgrade --install loki grafana/loki \
    --version 7.5.0 \
    --namespace "$NS" \
    -f "$ROOT/loki/values.yaml" --wait --timeout 10m
  helm upgrade --install alloy grafana/alloy \
    --version 1.15.0 \
    --namespace "$NS" \
    --values "$ROOT/loki/alloy.yaml" --wait --timeout 10m
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:3100/ready || true)"
  # Loki без NodePort снаружи — проверка через порт-форвард kubectl
  if [ "$code" != "200" ]; then
    kubectl -n "$NS" port-forward svc/loki 3100:3100 >/tmp/loki-pf.log 2>&1 &
    PF=$!; sleep 3
    code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:3100/ready || true)"
    kill $PF 2>/dev/null || true
  fi
  [ "$code" = "200" ] || { echo "loki: not ready ($code)"; exit 1; }
  echo "loki: ready; alloy: $(kubectl -n "$NS" get ds alloy -o jsonpath='{.status.numberReady}') ready"
}

section_demo() {
  # Окружения + GitOps: AppProject (граница prod/staging) + Application demo-prod (auto-sync).
  # ВАЖНО: Application смотрит targetRevision=main — запускать после merge инкремента в main.
  kubectl create namespace prod --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl create namespace staging --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl apply -f "$ROOT/../infra/argocd/appproject.yaml"
  kubectl apply -f "$ROOT/../infra/argocd/application-demo.yaml"
  kubectl -n "$NS" wait --for=jsonpath='{.status.sync.status}'=Synced application/demo-prod --timeout=300s
  kubectl -n prod wait --for=condition=Ready pod -l app=podinfo --timeout=300s
  echo "demo: podinfo Running in prod (argocd Synced)"
}

section_openproject() {
  # Трекер задач. Пароль админа — из Vault → secret openproject-admin (используется при ПЕРВОЙ установке).
  helm repo add openproject https://charts.openproject.org >/dev/null 2>&1 || true
  helm repo update openproject >/dev/null
  local pw
  pw="$(vcli kv get -field=password secret/openproject/admin 2>/dev/null || true)"
  [ -n "$pw" ] || { pw="$(openssl rand -hex 12)"; vcli kv put secret/openproject/admin username=op-admin "password=$pw" >/dev/null; }
  kubectl -n "$NS" create secret generic openproject-admin \
    --from-literal="password=$pw" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  helm upgrade --install openproject openproject/openproject \
    --namespace "$NS" \
    --values "$ROOT/openproject/values.yaml" --wait --timeout 20m
  kubectl -n "$NS" wait --for=condition=Available deployment/openproject --timeout=600s 2>/dev/null || true
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' http://localhost:30083/api/v3/status || true)"
  # OP v17-slim: password/basic-auth сломан (TODO) — живость проверяем наличием HTTP-ответа API
  case "$code" in
    200|400|401) echo "openproject: alive (api $code; auth-квест — TODO)" ;;
    000) echo "openproject: not reachable"; exit 1 ;;
    *) echo "openproject: unexpected ($code)"; exit 1 ;;
  esac
  echo "openproject: ready (ui http://localhost:30083)"
}

section_litellm() {
  # Шлюз моделей. Master key → Vault + secret; конфиг — model_list над хостовой Ollama.
  # Ollama на хосте: litellm-под резолвит хост через hostAliases (IP lima-хоста определяется динамически).
  local mk hostip
  mk="$(vcli kv get -field=master-key secret/litellm/master 2>/dev/null || true)"
  [ -n "$mk" ] || { mk="sk-lab-$(openssl rand -hex 12)"; vcli kv put secret/litellm/master "master-key=$mk" >/dev/null; }
  [ -n "$mk" ] || { echo "litellm: пустой master key — прерываю"; exit 1; }
  # colima/lima -> host.lima.internal; Docker Desktop / Rancher Desktop (Windows) -> host.docker.internal
  hostip="$(docker run --rm alpine sh -c 'getent hosts host.lima.internal || getent hosts host.docker.internal' | awk 'NR==1{print $1}')"
  [ -n "$hostip" ] || { echo "litellm: не удалось определить IP хоста (host.lima.internal / host.docker.internal)"; exit 1; }
  # БД для LiteLLM (ключи/teams хранятся там же)
  kubectl apply -f "$ROOT/postgres/litellm-database.yaml"
  kubectl -n "$NS" wait --for=jsonpath='{.status.ready}'=true database/litellm-db --timeout=180s 2>/dev/null || true
  local dbpw dburl
  dbpw="$(kubectl -n "$NS" get secret postgres-platform-app -o jsonpath='{.data.password}' | base64 -d)"
  dburl="postgresql://gitea:${dbpw}@postgres-platform-rw.$NS.svc.cluster.local:5432/litellm"
  kubectl -n "$NS" create secret generic litellm-db-url \
    --from-literal="url=$dburl" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n "$NS" create secret generic litellm-master \
    --from-literal="master-key=$mk" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl apply -f "$ROOT/litellm/deployment.yaml"
  kubectl -n "$NS" patch deploy litellm --type=strategic \
    -p "{\"spec\":{\"template\":{\"spec\":{\"hostAliases\":[{\"ip\":\"$hostip\",\"hostnames\":[\"host.lima.internal\"]}]}}}}" >/dev/null
  kubectl -n "$NS" rollout restart deploy/litellm
  kubectl -n "$NS" wait --for=condition=Available deployment/litellm --timeout=600s
  local code i
  code=""
  for i in $(seq 1 12); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $mk" -H 'Content-Type: application/json' \
      -d '{"model":"qwen-coder","messages":[{"role":"user","content":"ping"}],"max_tokens":16}' \
      http://localhost:30084/v1/chat/completions || true)"
    [ "$code" = "200" ] && break
    sleep 10
  done
  [ "$code" = "200" ] || { echo "litellm: completion failed ($code)"; exit 1; }
  echo "litellm: ready (api http://localhost:30084/v1)"
}

section_agents() {
  # Namespace + scratch PVC + network policy for agent sandbox pods (M4b).
  kubectl apply -f "$ROOT/agents/namespace.yaml"
  kubectl apply -f "$ROOT/agents/pvc.yaml"
  kubectl apply -f "$ROOT/agents/netpol-default-deny.yaml"
  kubectl apply -f "$ROOT/agents/netpol-allow.yaml"
  # k3d publishes only individually mapped host ports; add the sandbox
  # NodePort range to the load balancer once (live serverlb swap, no cluster
  # recreation). Guarded: skip when the range is already mapped.
  if command -v k3d >/dev/null 2>&1 && command -v docker >/dev/null 2>&1; then
    if ! docker port k3d-swarm-lab-serverlb 2>/dev/null | grep -q "30100/tcp"; then
      k3d cluster edit swarm-lab --port-add "30100-30110:30100-30110@loadbalancer" \
        && echo "agents: added k3d NodePort range 30100-30110"
    else
      echo "agents: k3d NodePort range 30100-30110 already mapped"
    fi
  else
    echo "agents: k3d/docker not found — skipping NodePort range mapping" >&2
  fi
  kubectl -n agents get pvc agent-scratch >/dev/null
  echo "agents: namespace, agent-scratch PVC, network policies Ready"
}

section_mcp() {
  # MCP servers (M4c): task-source (Gitea Issues) + llm (LiteLLM proxy) in `agents`.
  # The image is built on the host and imported into k3d: `agents` is
  # default-deny, so pods cannot pip-install at boot.
  local img="mcp-servers:0.1.0" gt mk
  docker build -q -f "$ROOT/mcp/Dockerfile" -t "$img" "$ROOT/../packages/mcp_servers" >/dev/null
  k3d image import "$img" -c swarm-lab >/dev/null
  # Credentials: OpenBao -> per-server k8s Secrets (values never echoed).
  gt="$(vcli kv get -field=token secret/gitea/admin 2>/dev/null || true)"
  [ -n "$gt" ] || { echo "mcp: no Gitea token at secret/gitea/admin (OpenBao sealed?)"; exit 1; }
  mk="$(vcli kv get -field=master-key secret/litellm/master 2>/dev/null || true)"
  [ -n "$mk" ] || { echo "mcp: no LiteLLM key at secret/litellm/master"; exit 1; }
  kubectl -n agents create secret generic mcp-task-source --from-literal="gitea-token=$gt"     --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n agents create secret generic mcp-llm --from-literal="api-key=$mk"     --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl apply -f "$ROOT/agents/netpol-allow.yaml"
  kubectl apply -f "$ROOT/agents/netpol-mcp.yaml"
  kubectl apply -f "$ROOT/mcp/task-source.yaml"
  kubectl apply -f "$ROOT/mcp/llm.yaml"
  # Same tag re-imported: restart so the pods pick up the fresh image.
  kubectl -n agents rollout restart deploy/task-source deploy/llm >/dev/null
  kubectl -n agents rollout status deploy/task-source --timeout=180s
  kubectl -n agents rollout status deploy/llm --timeout=180s
  # Host access for the worker's tracker loop: map NodePort 30085 once.
  if ! docker port k3d-swarm-lab-serverlb 2>/dev/null | grep -q "30085/tcp"; then
    k3d cluster edit swarm-lab --port-add "30085:30085@loadbalancer"       && echo "mcp: added k3d NodePort 30085"
  fi
  local code i
  code=""
  for i in $(seq 1 20); do
    code="$(curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost:30085/mcp       -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream'       -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' || true)"
    [ "$code" = "200" ] && break
    sleep 3
  done
  [ "$code" = "200" ] || { echo "mcp: task-source not answering on :30085 ($code)"; exit 1; }
  echo "mcp: task-source (:30085, task-source.agents.svc:8000) and llm (llm.agents.svc:8000) Ready"
}

case "${1:-all}" in
  vault)  section_vault ;;
  cnpg)   section_cnpg ;;
  cluster) bash "$ROOT/scripts/k3d-up.sh" ;;
  gitea)  section_gitea ;;
  runner) section_runner ;;
  argocd) section_argocd ;;
  monitoring) section_monitoring ;;
  loki)   section_loki ;;
  demo)   section_demo ;;
  openproject) section_openproject ;;
  litellm) section_litellm ;;
  agents) section_agents ;;
  mcp)    section_mcp ;;
  # `all` brings the k3d cluster up first so `org-bootstrap apply` works on a fresh machine.
  all)    bash "$ROOT/scripts/k3d-up.sh"; section_vault; section_cnpg; section_gitea; section_runner; section_argocd; section_monitoring; section_loki; section_demo; section_openproject; section_litellm; section_agents; section_mcp ;;
  *) echo "usage: $0 [cluster|vault|cnpg|gitea|runner|argocd|monitoring|loki|demo|openproject|litellm|agents|mcp|all]"; exit 2 ;;
esac
