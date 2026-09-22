# Скрипты

Роль каждого скрипта — чтобы при рефакторинге и подготовке клиента №2 было видно, что можно
трогать свободно, а что завязано на живой процесс. Ничего отсюда не удалять и не переносить
без явного решения — `test_scripts_help.py` проверяет, что все скрипты с `argparse` хотя бы
запускаются (`--help`), это не проверка их логики.

## Онбординг нового клиента

| Скрипт | Что делает |
|---|---|
| `create_kb_draft.py` | создаёт черновик клиентской KB из шаблона и исходных материалов |
| `validate_kb.py` | проверяет клиентскую базу знаний без запуска backend и зависимостей |
| `onboard_client.py` | публикует проверенную KB клиента в рабочую папку и печатает embed |
| `client_launch_check.py` | проверяет готовность опубликованного клиента к запуску |
| `crawl_article_batch.py` | краулит staging batch статей и режет текст на chunks для будущего RAG |
| `import_articles_registry.py` | импортирует реестр статей клиента в staging без публикации в runtime KB |

## Ежедневная проверка и отладка

| Скрипт | Что делает |
|---|---|
| `preflight.py` | одна команда «всё ли на месте»: отчёт `/api/debug/preflight`, красные линии, взгляд снаружи |
| `policy_snapshot.py` | сеть безопасности перед правкой правил: одни и те же сообщения через две версии кода, список различий и покрытие исходов |
| `dialog_snapshot.py` | сеть безопасности, диалоги целиком через HTTP: две версии кода по ходам диалога + красные линии |
| `debug_trace_batch.py` | пакетно прогоняет конфликтные сообщения через internal debug trace |
| `export_dialogs.py` | вытаскивает переписки по префиксам session_id в один текстовый файл |
| `smoke_ai_scenarios.py` | smoke-матрица AI/policy сценариев для любого опубликованного клиента |
| `run_ai_evals.py` | baseline eval runner для качества AI/policy понимания фраз |
| `vendor_compare.py` / `demo_gate.py` | сравнение с примерами диалогов вендора; `demo_gate` — та же проверка перед демо |
| `send_test_delivery.py` | отправляет тестовое delivery-событие для проверки каналов клиента |
| `smoke_managed.py` | smoke-проверка managed-service flow через FastAPI TestClient |
| `smoke_onboarding.py` | smoke-проверка полного onboarding flow без Docker и реальных клиентов |
| `smoke_article_retrieval.py` | проверяет retrieval по staging article chunks без embeddings/pgvector |
| `simulate_kb.py` | прогоняет тестовые вопросы на клиентской KB без запуска Docker |

## Личный инструмент

| Скрипт | Что делает |
|---|---|
| `kb_console.py` | интерактивная консоль для правки client KB без ручного лазания по yaml |

## Одноразовая сборка базы РОШ (сделано, для нового клиента не нужны)

| Скрипт | Что делает |
|---|---|
| `import_services_excel.py` | импортирует прайс XLSX в staging-данные без публикации в runtime KB |
| `select_public_services.py` | фильтрует staging-услуги в публичный набор для виджета |
| `build_service_groups.py` | собирает публичные услуги в направления с вариантами из прайса |
| `merge_service_groups.py` | добавляет новые service groups в уже существующие services.json/prices.json клиента |
| `map_service_group_urls.py` | связывает service groups с URL страниц услуг из CSV-реестра |
| `publish_rosh_import_demo.py` | публикует локального demo-клиента из импортированных групп услуг |
| `report_failed_sources.py` | формирует cleanup queue по failed RAG sources из updated manifest |

## Заготовка V2 (не используется сейчас)

| Скрипт | Что делает |
|---|---|
| `prepare_rag_pgvector_payload.py` | готовит staging JSONL payload для будущей загрузки RAG corpus в Postgres/pgvector |
