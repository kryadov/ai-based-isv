# Аддендум: UI-панель организации и Automations

Дата: 2026-11-08
Статус: утверждён владельцем в визуальном брейншторме (мокапы: `.superpowers/brainstorm/59323-1794099078/content/`)
Дополняет: `2026-11-07-swarm-dev-platform-design.md` (§7.1 UI, §10 M10)

## 1. UI-панель организации (agent_api web)

### Навигация (табы)
`📥 Inbox` (домашний) · `🕸 Swarm` · `🚚 Pipeline` · `⚡ Automations` · `📊 Metrics` · `🧠 Memory` · `⚙ Settings`

### Верхняя полоса
- Слева: бейджи состояния (проекты, активные агенты).
- Справа: переключатель языка **EN ▾ / RU** (выбор в профиле), пользователь + роль, **Logout**.

### Авторизация (решение)
Логин — **только для людей**: локальные пользователи в v0, OIDC/SSO позже. Агенты имеют собственные service-identity и в UI не логинятся — они видны как сущности на табах Swarm/Memory.

### 📥 Inbox (домашний экран)
Карточки решений человека; рой останавливается в этих точках (спека §7.7 HITL). Типы карточек: Plan approval · Merge · Canary promotion · Memory promotion · Escalation (вопрос агента) · Automation action (см. §2). Карточка показывает: что/в каком проекте/как долго ждёт/контекст-ссылки (spec, PR, evidence, diff, rollback plan) + действия. Внизу экрана — **полоса «Организация health»** (5 метрик, §урок 14): trusted change, time-to-accept, merge queue, agent burn/day, rule violations.

### 🕸 Swarm (детально)
Заголовок рана (проект, задача, бюджет, pause/escalate) + «org chart» рана: координатор → специалисты (review-роли) → исполнители вершин со статусами (done/running/queued/blocked), lease и расход бюджета. Клик по вершине → сайд-панель: done_when, paths_allow, агент+resolved_model, рестарты сессии, evidence, живой tail session log, kill / restart clean.

### 🚚 Pipeline
Канбан доставки: Задача → Спека → Код → Gates → Review → Canary → Prod; красные карточки = упавшие gates; деталь = PR + evidence + rollback plan.

### ⚡ Automations
См. §2.

### 📊 Metrics
Полные графики (trusted change, time-to-accept, DORA, verification debt, бюджеты) с drill-down по проектам.

### 🧠 Memory
Org-конституция, reuse-каталог, уроки; поиск; промоции и TTL; авторство записей.

### Язык
База интерфейса — английский; переключение на RU в шапке, выбор запоминается в профиле. Русские строки — ресурсный файл.

## 2. Automations (новая сущность control plane)

Рекуррентные/событийные организационные workflow — **тот же swarm-движок**, другой профиль запуска: событие/расписание → агент-специалист → отчёт/артефакт → (при необходимости) создание задач в трекере, которые идут полным конвейером доставки.

### Модель
- **Определение = код в репо** (версионируется, проходит VET): `trigger` (cron | webhook | manual | event), `scope` (все проекты / выбранные / сервисы), `agent-role` (специалист), `budget` (на прогон/день), шаблон workflow (skill).
- **Результат прогона**: отчёт (артефакт) + карточка в Inbox + записи в org-memory (уроки/находки).
- **Действия** (например, «создать N fix-задач») — только через **HITL-аппрув**; созданные задачи получают фидбек-петлю статусов (TODO: In Progress/Done/Blocked, комментарии этапов).

### Стартовый набор
| Automation | Триггер | Scope | Результат |
|---|---|---|---|
| vuln-scan | cron daily | все/выбранные проекты | отчёт уязвимостей; critical → карточка «create fix tasks» |
| dep-updates | cron weekly | выбранные | батч задач-обновлений (полный PR-flow) |
| log-analysis | cron daily / event | все сервисы | анализ логов (Loki) → аномалии → уроки в память |
| feedback-ingest | webhook + file upload | org | инсайты из фидбека кастомеров → задачи на фичи |

### Полный каталог (утверждён владельцем 2026-11-08; реализация движка — M3/M10, лёгкие случаи возможны раньше как CronJob)

**Код и качество**
| # | Automation | Триггер | Результат |
|---|---|---|---|
| 1 | maintainability-sweep | weekly | задачи-чистки (мёртвый код, сироты-тесты, TODO) |
| 2 | flaky-test-hunter | nightly | карантин/фикс нестабильных тестов |
| 3 | coverage-gap-analysis | weekly | задачи по критичным путям без тестов |
| 4 | architecture-drift | per-merge + monthly | блокировка + отчёт (ArchUnit-стиль) |
| 5 | dead-endpoint-detection | monthly | задачи на удаление (по трафику Prometheus) |
| 6 | tech-debt-register | quarterly | отчёт руководителю |
| 7 | docs-openapi-drift | per-merge | замечания в PR |
| 8 | spec-drift | weekly | задачи (`.specs/` vs код) |
| 9 | license-compliance | weekly | отчёт + блокирующие находки |

**Безопасность и комплаенс**
| 10 | secret-rotation-audit | monthly | Inbox (critical) |
| 11 | iam-least-privilege | monthly | карточка HITL |
| 12 | image-freshness | weekly | задачи |
| 13 | iac-drift | daily | алерт + задача |
| 14 | pii-gdpr-audit | monthly | отчёт комплаенсу |
| 15 | agent-safety-audit | monthly | отчёт платформенной команде |

**Операции и надёжность**
| 16 | post-mortem-drafter | event (инцидент) | Inbox → PR в docs |
| 17 | error-budget-tracking | daily | предупреждения |
| 18 | backup-restore-drill | monthly | отчёт ✓/✗ |
| 19 | cost-capacity-trends | weekly | отчёт + задачи |

**Продукт и рынок**
| 20 | usage-dead-features | monthly | задачи на удаление |
| 21 | experiment-analysis | event | отчёт |
| 22 | ecosystem-watch | weekly | дайджест |
| 23 | regulatory-watch | monthly | задачи на комплаенс |

**Организационная память**
| 24 | memory-hygiene | weekly | PR-ы в орг-память (консолидация, TTL, дрейф) |
| 25 | onboarding-pack-generator | event | PR + сообщение |

**Мета: платформа улучшает себя**
| 26 | agent-performance-review | weekly | предложения правок skills/harness |
| 27 | harness-meta-fixer | event (сбой >2 раз) | PR в infra/skills |
| 28 | eval-corpus-growth | continuous | PR в packages/evals |
| 29 | org-digest | weekly | сводка в Slack/почту |

Итого: 26 в каталоге + 4 стартовых = 30 автоматизаций.

### UI
Таб ⚡ Automations: список (name, trigger, scope, last result, next run, budget, on/off) + деталь прогона: таблица находок (project/severity/CVE/fix est.) с чекбоксами, кнопка «Approve & create N fix tasks», история прогонов.

## 3. Что оставлено билдеру (M10)

- Фронтенд-технология панели (React поверх agent_api API — решение при старте M10).
- Компонентная библиотека/стиль (мокапы — wireframe-уровень).
- Хранение Language preference и профилей пользователей.
