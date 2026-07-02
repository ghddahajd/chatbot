# Project map

Короткая навигация по проекту, чтобы не искать нужный файл среди README,
скриптов, evals и рабочих заметок.

## Главные точки входа

- `README.md` — запуск проекта, embed-код, LLM-настройки, основные проверки.
- `README_ONBOARDING.md` — короткая инструкция подключения нового клиента.
- `CLIENT_ONBOARDING_PLAYBOOK.md` — полный рабочий процесс: что спросить у клиента, как завести KB, как проверить запуск.
- `CLIENT_LAUNCH_CHECKLIST.md` — чеклист перед отдачей клиенту.
- `AGENTS.md` — правила для AI-агента в этом репозитории. Локальный рабочий файл, не часть клиентской документации.

## Runtime-код

- `backend/app/main.py` — сборка FastAPI-приложения и подключение роутов.
- `backend/app/services/chat_service.py` — основной оркестратор обработки сообщения.
- `backend/app/policy/` — защитный слой: intent, цены, услуги, restricted advice, operator/lead flow.
- `backend/app/llm/` — LLM-клиенты, prompts, structured classifier, parsing.
- `backend/app/knowledge.py` — загрузка KB клиента и безопасный resolver путей.
- `backend/app/delivery.py` — generic delivery outbox для lead/booking/operator events.
- `backend/app/routes/` — HTTP/WebSocket endpoints.
- `widget/widget.js` — one-script виджет на Vanilla JS + Shadow DOM.

## Данные и шаблоны клиентов

- `backend/data/client_template/universal_sample/` — нейтральный шаблон клиента.
- `backend/data/client_template/medical_sample/` — пример medical/beauty клиента.
- `backend/data/client_template/auto_service_demo/` — пример автосервиса.
- `backend/data/clients/` — локальные реальные/тестовые KB клиентов. Папка ignored, кроме `.gitkeep`.
- `backend/data/defaults/` — общие дефолты для клиентов.

## Проверки и скрипты

- `backend/scripts/create_kb_draft.py` — создать черновик KB клиента.
- `backend/scripts/validate_kb.py` — проверить структуру KB.
- `backend/scripts/onboard_client.py` — dry-run/publish клиента.
- `backend/scripts/client_launch_check.py` — итоговая проверка готовности клиента.
- `backend/scripts/smoke_onboarding.py` — smoke onboarding flow.
- `backend/scripts/smoke_managed.py` — managed-service smoke.
- `backend/scripts/smoke_ai_scenarios.py` — доменные smoke-сценарии AI.
- `backend/scripts/run_ai_evals.py` — строгие AI evals по JSONL.
- `backend/scripts/debug_trace_batch.py` — пакетный debug trace конфликтных фраз.
- `backend/scripts/send_test_delivery.py` — тест lead/booking/operator delivery.

## Evals и debug

- `backend/evals/*.jsonl` — strict AI evals: intent/action/service_id/markers. Коммитятся как regression-набор.
- `evals/universal.yaml` — universal smoke-сценарии для любого клиента.
- `evals/domains/*.yaml` — доменные smoke-сценарии.
- `evals/clients/` — локальные client-specific evals. Ignored, потому что там могут быть реальные фразы клиента.
- `/debug?token=...` — ручная debug-панель decision trace.
- `tasks/DEBUG_TRACE_REVIEW.md` — локальный журнал спорных фраз. Ignored.

## Рабочие и ignored-папки

- `tasks/` — локальные планы, заметки, V2-roadmap, debug trace runs. Не считать публичной документацией.
- `docs/` — локальные/рабочие документы. Сейчас ignored.
- `new/` — drafts KB и временные материалы onboarding.
- `test-sites/` — внешние сайты для проверки embed.
- `backend/logs/` — runtime jsonl: leads, analytics, delivery outbox.

## Минимальный путь подключения клиента

1. Создать draft:

```bash
.venv/bin/python backend/scripts/create_kb_draft.py \
  --company-id client_id \
  --company-name "Название клиента" \
  --city "Москва" \
  --phone "+7 000 000-00-00"
```

2. Заполнить файлы в `new/kb_drafts/client_id/`.
3. Проверить:

```bash
.venv/bin/python backend/scripts/onboard_client.py client_id --dry-run
```

4. Опубликовать:

```bash
.venv/bin/python backend/scripts/onboard_client.py client_id --publish
```

5. Перезапустить backend.
6. Проверить:

```bash
.venv/bin/python backend/scripts/client_launch_check.py --company=client_id
.venv/bin/python backend/scripts/smoke_ai_scenarios.py --company=client_id
.venv/bin/python backend/scripts/send_test_delivery.py --company=client_id --event=all --dry-run
```

7. Вставить script на сайт клиента.

## Архитектурный статус

Проект сейчас выглядит нормально для managed-service MVP:

- Есть разделение `KB -> policy -> LLM -> validator/output`, LLM не является источником истины.
- Клиентские данные scoped by `company_id`, bootstrap не делает fallback на чужую KB.
- Widget изолирован через Shadow DOM и остаётся one-script embed.
- Delivery вынесен в отдельный outbox-слой, а не размазан по policy.
- Evals/debug уже есть, поэтому качество можно улучшать от реальных фраз.

Технический долг есть, но он не блокирует первого клиента:

- `policy/__init__.py`, `knowledge.py`, `llm/openai_compatible.py`, `widget/widget.js` крупные и требуют аккуратной декомпозиции позже.
- Есть два eval-слоя: `backend/evals` для strict AI checks и `evals` для smoke. Это допустимо, но важно держать назначение явно описанным.
- Sessions/leads/logs in-memory/jsonl. Для managed MVP нормально, для V2 нужен PostgreSQL.
- Operator panel простой и inline-heavy. Для MVP подходит, позже лучше либо вынести UI, либо интегрировать готовый helpdesk.

Главный риск сейчас не архитектурный, а операционный: не потерять порядок запуска
клиента и не забыть прогонять checks после изменения KB.
