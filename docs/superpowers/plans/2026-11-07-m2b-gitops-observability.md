# M2b: GitOps + наблюдаемость (Argo CD, kube-prometheus-stack, Loki) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:executing-plans (native — устоявшийся выбор владельца). Шаги с чекбоксами.

**Goal:** GitOps-выкат и наблюдаемость: Argo CD + kube-prometheus-stack (Prometheus/Grafana) + Loki на k3d; приёмка — demo-приложение задеплоено Argo CD в `prod`, его метрики видны в Prometheus.

**Architecture:** все компоненты в namespace `platform` (спека §6); окружения — namespace'ы `prod`/`staging`; граница окружений — Argo CD AppProject (destinations white-list). Demo-приложение (podinfo, multi-arch, `/metrics`) — манифесты в этом же репо `infra/demo-app/`, Argo CD Application следит за GitHub main (auto-sync). Loki получает логи через Alloy daemonset; Grafana с provisioned datasources (Prometheus+Loki). Версии чартов пинятся в `infra/env.yaml`.

**Tech Stack:** helm charts argo/argo-cd, prometheus-community/kube-prometheus-stack, grafana/loki, grafana/alloy; ghcr.io/stefanprodan/podinfo.

**Spec:** `docs/superpowers/specs/2026-11-07-swarm-dev-platform-design.md` (§6, §10 M2b; границы prod — §7.2.6, §7.7)

## Global Constraints

- namespace'ы: Argo CD / мониторинг / Loki — `platform`; demo — `prod` (критерий), `staging` — создаётся, без приложений.
- Порты наружу: Argo CD UI `30081`, Grafana `30082` (k3d port-mappings в `infra/k3d/config.yaml`).
- Версии чартов пинятся при установке и попадают в `infra/env.yaml` (через `scripts/gen-env.sh`); `latest` запрещён.
- Идемпотентность: `./infra/bootstrap.sh` повторно — без разрушения данных (helm upgrade --install + wait).
- Арго управляет только этим GitHub-репо (AppProject sourceRestriction) до появления Gitea-GitOps (M5+).
- Требование владельца: md-доки обновляются в этом инкременте (INSTALL: UI-доступы, acceptance).

## Review Focus

1. Повторный bootstrap не должен пересоздавать PVC Prometheus/Loki (данные метрик/логов выживают).
2. Argo CD Application должен честно синкаться из git: правка манифеста демо в ветке должна отражаться в prod (auto-sync проверяется в Task 6 изменением реплики).
3. Метрики podinfo в Prometheus — не «target up», а конкретная метрика приложения (`podinfo_requests_total` присутствует в instant-query).
4. AppProject не разрешает deploy вне prod/staging (destination white-list проверяется фактом манифеста; негативный тест — Task 7, попытка Application в чужой namespace отклоняется… если невозможно проверить без риска — checked-факт манифеста).
5. Пароль админа Grafana/ArgoCD не в git (ArgoCD — из `argocd-initial-admin-secret`, Grafana — генерируется чартом в secret; INSTALL описывает получение).

---

### Task 1: Порты + ветка

- [ ] Step 1: `infra/k3d/config.yaml` — добавить маппинги `30081:30081`, `30082:30082` (loadbalancer).
- [ ] Step 2: `./infra/scripts/k3d-up.sh` — кластер уже есть: **k3d не пересоздаёт маппинги у живого кластера** (Ruling: пересоздание кластера снёс бы Vault/PG/Gitea-данные… на самом деле PVC локальные и пересоздание кластера их теряет). Решение: `k3d cluster delete swarm-lab && ./infra/bootstrap.sh` — полный переразвод стенда с нуля, что заодно доказывает воспроизводимость L0 с нуля (полезная приёмка). Expected: все секции зелёные, PR-flow тест снова PASS.
- [ ] Step 3: ветка `m2b-gitops-observability`. Commit: `feat(m2b): k3d ports for argocd/grafana`

### Task 2: Argo CD

**Files:** Create `infra/argocd/values.yaml`; Modify `infra/scripts/bootstrap.sh` (section_argocd), `infra/scripts/gen-env.sh`.

- [ ] Step 1: values.yaml — chart argo/argo-cd; `server.insecure: true` (за NodePort без TLS), `configs.params."server\.insecure"=true` по схеме чарта; resource limits скромные.
- [ ] Step 2: bootstrap section_argocd: helm upgrade --install argocd argo/argo-cd -n platform --wait; wait deploy argocd-server.
- [ ] Step 3: проверка — UI `http://localhost:30081` отвечает (200/307); пароль админа: `kubectl -n platform get secret argocd-initial-admin-secret`.
- [ ] Step 4: Commit `feat(m2b): argo cd`

### Task 3: kube-prometheus-stack

**Files:** Create `infra/monitoring/values.yaml`; Modify bootstrap.sh (section_monitoring), gen-env.sh.

- [ ] Step 1: values.yaml — chart prometheus-community/kube-prometheus-stack: grafana NodePort 30082 (`grafana.service.type=NodePort,nodePort=30082`), grafana adminPassword через существующий secret (`grafana.admin.existingSecret` — создаётся bootstrap'ом с паролем в Vault), prometheus/prometheusSpec retention 3d, scrape pod annotations + ServiceMonitors; Loki datasource provisioning в grafana.additionalDataSources.
- [ ] Step 2: bootstrap section_monitoring (+ secret grafana-admin, пароль → Vault `secret/grafana/admin`).
- [ ] Step 3: проверка — Prometheus API (port-forward) `/-/healthy` OK; Grafana UI 30082 логин работает (curl /api/health с basic-auth).
- [ ] Step 4: Commit `feat(m2b): kube-prometheus-stack with grafana nodeport`

### Task 4: Loki + Alloy

**Files:** Create `infra/loki/values.yaml`, `infra/loki/alloy.yaml`; Modify bootstrap.sh (section_loki).

- [ ] Step 1: loki chart (single-binary, storage 5Gi) + Alloy daemonset (grafana/alloy), Alloy config:收集 pod logs → Loki (app.kubernetes.io/part-of фильтр не нужен — все поды), label pod/namespace.
- [ ] Step 2: bootstrap section_loki (два helm-релиза: loki, alloy) + wait.
- [ ] Step 3: проверка — Loki ready endpoint; в Loki есть лейблы от Alloy (query `{job="alloy"}` пусто-но-200 допустимо; строгая проверка — после demo-deploy в Task 6).
- [ ] Step 4: Commit `feat(m2b): loki + alloy log collection`

### Task 5: Demo-app + Argo CD Application + AppProject

**Files:** Create `infra/demo-app/prod/` (deployment, service, servicemonitor podinfo), `infra/argocd/appproject.yaml`, `infra/argocd/application-demo.yaml`; Modify bootstrap.sh (section_demo: kubectl apply ns prod/staging + appproject + application).

- [ ] Step 1: манифесты podinfo: Deployment 1 replica, image `ghcr.io/stefanprodan/podinfo:<pinned>`, Service с port metrics? podinfo отдаёт /metrics на том же порту 9898; ServiceMonitor с podMetricsEndpoints.
- [ ] Step 2: AppProject `swarm-lab`: sourceRepo https://github.com/kryadov/ai-based-isv, destinations [prod, staging] in-cluster.
- [ ] Step 3: Application `demo-prod`: source path infra/demo-app/prod, targetRevision main, destination prod, syncPolicy automated+prune+selfHeal.
- [ ] Step 4: bootstrap section_demo: `kubectl apply ns prod/staging` + AppProject + Application; wait Synced.
- [ ] Step 5: проверка — pod Running в prod; Argo CD app status `Synced`+`Healthy`.
- [ ] Step 6: Commit `feat(m2b): demo app via argocd into prod`

### Task 6: Приёмка — auto-sync + метрики + логи

**Files:** Create `infra/tests/m2b_deploy_flow.sh`; Modify bootstrap.sh (вызов в правильном порядке) + gen-env.sh (добавить argocd/kube-prometheus-stack/loki/podinfo версии).

- [ ] Step 1: m2b_deploy_flow.sh: (a) Argo CD app demo-prod status Synced+Healthy (через argocd API: `GET /api/v1/applications/demo-prod` c admin token из argocd-initial-admin-secret… пароль admin; auth via /api/v1/session); (b) podinfo pod Running в prod; (c) Prometheus instant-query `podinfo_requests_total` → непустой результат; (d) Loki query `{namespace="prod"}` → есть строки; (e) auto-sync: правка replicas 1→2 через GitHub API commit → wait Argo CD применит → в prod 2 пода → revert. Каждый шаг с fail().
- [ ] Step 2: прогон — Expected: «M2b deploy-flow: PASS».
- [ ] Step 3: ген-env.sh расширить; повторный полный bootstrap — Review Focus #1 (PVC живут).
- [ ] Step 4: Commit `test(m2b): deploy-flow acceptance (argocd sync, prometheus metrics, loki logs, autosync)`

### Task 7: Доки + финал

- [ ] Step 1: INSTALL.md — раздел 4 дополнить: Argo CD UI 30081 (admin/password из argocd-initial-admin-secret), Grafana 30082 (admin, пароль в Vault), acceptance `./infra/tests/m2b_deploy_flow.sh`; раздел 6 — добавить кейсы.
- [ ] Step 2: тройка верификации + оба acceptance-теста (m2a, m2b) зелёные.
- [ ] Step 3: PR `m2b-gitops-observability` → main → CI зелёный → merge (флоу M1/M2a).
- [ ] Step 4: финальный отчёт: evidence + rulings.
