# Туториал: работа с платформой на лабораторном стенде

> English version: [TUTORIAL.md](TUTORIAL.md)

Путь от «стенд поднят» до «агент выкатил моё изменение». Все команды проверены на текущем стенде (Windows + Rancher Desktop + toolbox, AGENTS.ru.md §11). Запускайте их **из Git Bash в корне репо**. Всё, что требует Python, kubectl-инструментов или uv, идёт через `./infra/scripts/toolbox.sh`: нативный `python3` на Windows — заглушка из Store, которая зависает.

Что делает платформа, в одну строку:

**issue в трекере → агент в k3s-песочнице → PR → CI (тесты + security) → бот-ревью → раунды исправлений → ваше одобрение (HITL) → мерж → issue закрыт**

Как это устроено внутри: [ARCH.ru.md](ARCH.ru.md) §3 (конвейер) и §3a (самообеспечение). Правила: [AGENTS.ru.md](AGENTS.ru.md). Текущее состояние: [STATE.md](STATE.md).

---

## 1. Проверить стенд

После перезапуска Rancher Desktop OpenBao (секреты) поднимается **запечатанным**. Сначала распечатайте его; команда идемпотентна, повторять безопасно:

```bash
./infra/scripts/toolbox.sh bash infra/scripts/bootstrap.sh vault
```

Затем проверьте инструменты и основные сервисы:

```bash
./infra/scripts/toolbox.sh uv run org-bootstrap check
kubectl -n platform get pods          # gitea, litellm, argocd, prometheus... всё Running
```

Если чего-то нет, перезапустите его секцию (все секции идемпотентны):

```bash
./infra/scripts/toolbox.sh bash infra/scripts/bootstrap.sh <секция>
```

Список секций — в [INSTALL.ru.md](INSTALL.ru.md).

## 2. Секреты и веб-интерфейсы

Все пароли и токены лежат в OpenBao. Хелпер на текущую сессию shell:

```bash
bao_get() {  # использование: bao_get <путь> <поле>
  ./infra/scripts/toolbox.sh bash -c "kubectl -n platform exec \$(kubectl -n platform get pod -l app.kubernetes.io/name=openbao -o name) -- env BAO_ADDR=http://127.0.0.1:8200 BAO_TOKEN=\$(cat infra/.vault-root-token) bao kv get -field=$2 $1"
}
bao_get secret/gitea/admin password
```

| Что | Где | Вход |
|---|---|---|
| Gitea (репо, issues, PR агентов, прогоны CI) | http://localhost:30080 | `swarm-admin` / `bao_get secret/gitea/admin password` |
| Argo CD | http://localhost:30081 | `admin` / `kubectl -n platform get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' \| base64 -d` |
| Grafana (метрики; логи Loki — в Explore) | http://localhost:30082 | `admin` / `bao_get secret/grafana/admin password` |
| LiteLLM (шлюз моделей, OpenAI API) | http://localhost:30084/v1 | Bearer `bao_get secret/litellm/master master-key` |

Маршрутизация моделей: все вызовы идут в z.ai GLM Coding Plan. OpenRouter используется **только** после исчерпания лимита z.ai (AGENTS.ru.md §11). Ключи провайдеров — в `.env` в корне репо (шаблон `.env.example`), никогда не в git.

## 3. Посмотреть, как одна задача проходит весь путь (5 минут)

```bash
./infra/scripts/toolbox.sh bash infra/tests/m5_flow.sh
```

Что делает флоу:
1. засевает маленький Python-проект `swarm-lab/target-scratch`;
2. открывает в нём issue («добавить `reverse_words`»);
3. запускает агента (OpenCode + GLM через LiteLLM) в поде-песочнице в namespace `agents`;
4. открывает PR, который проходит CI (`ci / checks`, `ci / security`) и бот-ревью;
5. сам одобряет HITL-запрос на мерж — **автоодобрение есть только в этом демо-флоу** — и PR мержится;
6. закрывает issue.

В конце печатается `M5 flow: PASS`.

Результат смотрите в Gitea, в `swarm-lab/target-scratch`:
- закрытый issue с комментариями `[platform] status:`;
- смерженный PR, комментарии ревью и прогон CI во вкладке **Actions**.

Доказательства лежат в `.agent-runtime-workspace/run-<N>/`:
- `worker.log`;
- `.agent_runtime/runs/run-<N>/vertex-<N>/session_log.ndjson` — полный поток событий агента;
- `manifest.json` — образ, модель, статистика диффа и вердикт.

## 4. Входящие HITL: мержи одобряете вы

Каждый нетривиальный мерж ждёт человека. Пока флоу работает, его control plane (`agent_api`) слушает порт 8472 в сети toolbox:

```bash
./infra/scripts/toolbox.sh curl -s http://127.0.0.1:8472/hitl/pending
./infra/scripts/toolbox.sh curl -s -X POST http://127.0.0.1:8472/hitl/<id>/decision \
  -H 'Content-Type: application/json' -d '{"action":"approve","decided_by":"<ваше имя>"}'
```

Чтобы отказать, передайте `"action":"reject"`. Перед решением прочитайте в Gitea дифф PR, его CI и бот-ревью. CLI-команда и веб-панель — в планах (TODO.ru.md).

## 5. Свой проект

### 5.1 Создать репо с CI-коридором

```bash
./infra/scripts/toolbox.sh uv run org-bootstrap onboard --project my-svc --stack python --push-whitelist swarm-admin
```

Команда:
- создаёт `swarm-lab/my-svc`;
- раскладывает check-профиль (`.gitea/workflows/ci.yml`: ruff, mypy, pytest, gitleaks, semgrep) и `.platform/merge-policy.yaml`;
- защищает `main`: мерж только при зелёном CI;
- с `--push-whitelist swarm-admin` разрешает вам запушить начальный код напрямую;
- записывает проект в `infra/projects.yaml`.

Агенты работают от не-админского `swarm-agent`, у которого есть запись во все репо `swarm-lab`.

### 5.2 Запушить код

Коду нужны `pyproject.toml` и тесты pytest: python-профиль выполняет `uv sync --all-groups`, затем ruff, mypy и pytest.

```bash
git clone http://localhost:30080/swarm-lab/my-svc.git && cd my-svc
# добавить код и тесты, затем:
git add . && git commit -m "initial code" && git push   # пользователь swarm-admin, пароль из bao_get
```

### 5.3 Дать задачу агенту

Флоу самообеспечения работает с любым репо `swarm-lab` через `SELFDEV_REPO`:

```bash
SELFDEV_REPO=my-svc \
SELFDEV_TITLE="Add /version endpoint" \
SELFDEV_BODY="Add GET /version returning {\"version\": \"0.1.0\"} in src/my_svc/app.py, with a pytest test in tests/test_version.py." \
SELFDEV_VERTEX="add GET /version endpoint returning the package version; test in tests/test_version.py" \
SELFDEV_DONE_CMD="uv sync --quiet --all-groups && uv run --quiet pytest -q" \
SELFDEV_PATHS="src/**,tests/**" \
./infra/scripts/toolbox.sh bash infra/tests/selfdev_flow.sh
```

Что означают переменные:
- `SELFDEV_VERTEX` — короткая строка задачи, не больше 120 символов;
- `SELFDEV_BODY` — полный текст: это issue, который читает агент;
- `SELFDEV_DONE_CMD` — приёмочная проверка, выполняется на хосте до открытия PR;
- `SELFDEV_PATHS` — файлы, которые агенту разрешено трогать.

Тексты задач лучше писать по-английски: так их надёжнее понимает модель.

Когда флоу напечатает `HITL merge request #<id> PENDING`, посмотрите PR в Gitea и одобрите его (§4). Дальше флоу дождётся мержа и закончится строкой `Selfdev flow: PASS`.

Именно этот путь проверен на репо самой платформы. Для новых проектов работает тот же код.

## 6. Задача для самой платформы (самообеспечение)

Платформа развивается через тот же коридор (AGENTS.ru.md §12). Канон — GitHub; рабочая копия агентов — Gitea `swarm-lab/ai-based-isv`.

```bash
bash infra/scripts/platform-sync.sh pull        # Git Bash на хосте: fast-forward Gitea из GitHub
SELFDEV_TITLE=... SELFDEV_BODY=... SELFDEV_VERTEX=... SELFDEV_PATHS=... \
  ./infra/scripts/toolbox.sh bash infra/tests/selfdev_flow.sh
# одобрить HITL-запрос (§4), прочитав PR
bash infra/scripts/platform-sync.sh promote     # открывает PR на GitHub selfdev/<sha7>
gh pr merge <N> --merge                         # после зелёного GitHub CI
bash infra/scripts/platform-sync.sh pull
```

Первое изменение платформы, сделанное так: Gitea PR #3 → GitHub PR #29 (релиз v0.6.0).

## 7. Если что-то не так

| Симптом | Что делать |
|---|---|
| После перезапуска падает всё, что связано с секретами | `./infra/scripts/toolbox.sh bash infra/scripts/bootstrap.sh vault` (распечатать) |
| `python3` зависает на Windows | это заглушка Store — используйте `./infra/scripts/toolbox.sh ...` |
| `something already listens on port 8472` | ещё работает предыдущий флоу, либо задайте `M4C_API_PORT=<свободный порт>` |
| Флоу падает с `done_when ... exited 2` | приёмочная команда не смогла собрать тесты (например, workspace не синхронизирован) — поправьте `SELFDEV_DONE_CMD` |
| `platform-sync.sh pull` выходит с кодом 3 (`unpromoted`) | в Gitea есть смерженная работа, которой нет на GitHub — сначала `promote` |
| Не работают вызовы моделей | проверьте ключи в `.env` → `bootstrap.sh litellm`; ошибки z.ai, кроме исчерпания лимита, намеренно не уходят в OpenRouter |

## 8. Выкат в prod: канарейка с анализом метрик (M6)

Демо-сервис `swarm-lab/isv-product` (засевается `bootstrap.sh demo`) работает в namespace `prod` как **канарейка Argo Rollouts**: Argo CD разворачивает его из каталога `deploy/` репозитория.

- Трафик: k6 даёт ровные 10 rps.
- Метрики: Prometheus (http://localhost:30090) и Grafana.

Когда коридор смержил изменение и выкат включён, компонент выката платформы (деплоер):
1. собирает `isv-product:<sha7>` из смерженного `main`;
2. снимает базовую линию до выката (доля 5xx, p95);
3. переключает Argo CD Application на новый образ.

Затем Argo Rollouts переводит трафик 20 → 50 → 100 %, параллельно идёт анализ:
- **канарейка против стабильной версии:** у канарейки должен быть трафик, её доля 5xx ≤ max(2 × стабильная, 5 %), p95 ≤ max(1,5 × стабильная, 300 мс);
- **окно после выката:** весь сервис сравнивается с базовой линией.

Если анализ провален, выкат прерывается, prod возвращается на стабильную версию, а в evidence появляется черновик `postmortem.md`. Выкат без пройденного анализа никогда не считается успешным.

```bash
./infra/scripts/toolbox.sh bash infra/tests/m6_flow.sh   # хороший путь (изменение агента) + плохой (ошибки → откат)
kubectl -n prod get rollout isv-product                  # фаза, шаг
kubectl -n prod get analysisrun                          # вердикты анализа
```

Выкатить коммит вручную или показать откат с инъекцией ошибок:

```bash
./infra/scripts/toolbox.sh uv run python -m agent_runtime.deployer --sha <sha> --repo-dir <клон> \
  [--fault FAULT_ERROR_RATE=0.5] --evidence-dir /tmp/deploy
```

**Конфиг выката меняет только человек.** `deploy/**` — защищённый путь гейтов, агент его изменить не может. Меняйте его операторским PR в Gitea. Первый выкат сервиса в prod тоже требует одобрения HITL (гейт №3).

Агенты видят выкаты и метрики через MCP-сервер `observability`, доступный только на чтение (`get_rollout`, `service_slo`, `query_metric`).

## 9. Чего пока нет

- Память проектов — M7. Постмортем отката пока черновик в evidence.
- Параллельный рой — M8.
- Веб-панель — M10.

См. [STATE.md](STATE.md) и [TODO.ru.md](TODO.ru.md).
