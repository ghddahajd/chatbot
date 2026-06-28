# Chat Widget MVP

Встраиваемый AI-чат для сайта медицинского / косметологического центра.

Виджет подключается одной строкой через `<script>`, отвечает по локальной базе знаний, не даёт медицинские рекомендации и умеет передавать диалог оператору.

## Что внутри

- `Vanilla JS` виджет на `Shadow DOM`
- backend на `FastAPI`
- база знаний в `backend/data`
- policy guard перед LLM
- `MockLLM` без ключей по умолчанию
- поддержка Gemini / OpenAI-compatible / Ollama
- лиды в `backend/logs/leads.jsonl`
- operator panel + WebSocket handoff
- demo-страница и пример внешнего сайта

## Запуск

```bash
cp .env.example .env
docker compose up --build
```

После запуска:

- внешний demo-сайт: `http://localhost:8000/demo/external-site.html`
- простое demo: `http://localhost:8000/demo/demo.html`
- operator panel: `http://localhost:8000/operator?token=demo-operator-token`
- healthcheck: `http://localhost:8000/health`

Остановить:

```bash
docker compose down
```

## Локально без Docker

```bash
cd backend
pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --port 8000
```

## Как встроить виджет

```html
<script
  src="http://localhost:8000/static/widget.js"
  data-company-id="rosh_demo"
  defer
></script>
```

Если backend живёт на другом домене, можно передать base URL:

```html
<script
  src="https://api.example.com/static/widget.js"
  data-company-id="rosh_demo"
  data-api-base="https://api.example.com"
  defer
></script>
```

Для клиента меняется только `data-company-id` и домен backend-а. Если `company_id`
указан с ошибкой, bootstrap вернёт `404`, а виджет не запустится на чужой базе.

## Основной flow

```text
AI_ACTIVE -> WAITING_OPERATOR -> HUMAN_ACTIVE -> CLOSED
```

- `AI_ACTIVE` — бот отвечает сам
- `WAITING_OPERATOR` — пользователь ждёт специалиста, AI молчит
- `HUMAN_ACTIVE` — оператор пишет в виджет напрямую
- `CLOSED` — диалог завершён

Сессия хранится в `localStorage`, поэтому после перезагрузки страницы виджет восстанавливает историю и текущий статус.

## LLM

По умолчанию всё работает без внешнего API:

```env
LLM_PROVIDER=mock
```

Gemini:

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_key
LLM_MODEL=gemini-3.5-flash
```

OpenAI-compatible endpoint:

```env
LLM_PROVIDER=openai_compatible
LLM_API_KEY=your_key
LLM_BASE_URL=https://your-endpoint.example/v1
LLM_MODEL=your-model-name
```

Ollama локально:

```bash
ollama pull qwen2.5:3b
ollama serve
```

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=qwen2.5:3b
LLM_API_KEY=local
LLM_SKIP_CLASSIFIER_FOR_LOCAL=true
```

## Что проверить руками

- `Сколько стоит лазерная эпиляция?`
- `Хочу чистку`
- `Что пить от прыщей?`
- `Есть ботокс?`
- `Я не из Москвы`
- `Позовите оператора`
- `Иван, +7 999 123-45-67`
- взять чат в operator panel и ответить клиенту

## Где менять данные

Для managed-service клиента база лежит локально и не коммитится:

```text
backend/data/clients/<company_id>/
├── company.yaml
├── services.json
├── prices.json
└── faq.md
```

Шаблон:

```bash
cp -R backend/data/client_template/sample_client backend/data/clients/new_client
```

После копирования поменяй `company_id` в `company.yaml` на название папки, например `new_client`.

Проверка KB без Docker:

```bash
python3 backend/scripts/validate_kb.py backend/data/clients/new_client
```

Общие дефолты лежат в `backend/data/defaults/`. Если в клиентском `company.yaml`
нет общего поля вроде `medical_disclaimer`, backend и валидатор возьмут его оттуда.

В embed-коде укажи тот же `company_id`:

```html
<script
  src="https://api.example.com/static/widget.js"
  data-company-id="new_client"
  data-api-base="https://api.example.com"
  defer
></script>
```

Старые файлы `backend/data/company.yaml`, `services.json`, `prices.json`, `faq.md` остаются fallback для demo.

## Как добавить клиента

1. Скопировать шаблон:

```bash
cp -R backend/data/client_template/sample_client backend/data/clients/client_id
```

2. В `backend/data/clients/client_id/company.yaml` поменять:

```yaml
company_id: client_id
company_name: Название клиента
city: Город
website_url: https://client-site.example
telegram_url: https://t.me/client
lead_webhook_url:
```

3. Заполнить `services.json`, `prices.json`, `faq.md`.

4. Проверить базу:

```bash
python3 backend/scripts/validate_kb.py backend/data/clients/client_id
```

5. Перезапустить backend, если он уже был запущен:

```bash
docker compose down
docker compose up --build
```

Сейчас KB грузится при старте backend-а. Файлы лежат volume-ом, поэтому пересобирать
образ из-за данных не обязательно, но процесс нужно перезапустить.

## Черновик KB из материалов клиента

Если клиент прислал сайт/тексты, сначала делаем draft в ignored-папке `new/`,
а не сразу в боевые `backend/data/clients/`:

```bash
python3 backend/scripts/create_kb_draft.py \
  --company-id client_id \
  --company-name "Название клиента" \
  --city "Москва" \
  --phone "+7 000 000-00-00" \
  --website-url "https://client-site.example" \
  --source "https://client-site.example/services" \
  --source "./materials/client-faq.md"
```

Результат:

```text
new/kb_drafts/client_id/
├── company.yaml
├── services.json
├── prices.json
├── faq.md
└── REVIEW.md
```

Важно: draft не публикуется автоматически. Его нужно вручную проверить, убрать
лишнее из `faq.md`, заполнить реальные `services.json` / `prices.json`, затем
прогнать проверки.

Проверить структуру:

```bash
python3 backend/scripts/validate_kb.py new/kb_drafts/client_id
```

Прогнать типовые вопросы без Docker:

```bash
cd backend && pip install -r requirements.txt
cd ..
python3 backend/scripts/simulate_kb.py new/kb_drafts/client_id
```

Свои вопросы:

```bash
python3 backend/scripts/simulate_kb.py new/kb_drafts/client_id \
  --question "покажи услуги" \
  --question "сколько стоит консультация" \
  --question "есть ботокс?"
```

Только после review draft можно копировать:

```bash
cp -R new/kb_drafts/client_id backend/data/clients/client_id
```

## Лиды и доставка

Лиды пишутся локально:

```text
backend/logs/leads.jsonl
```

Доставка в Telegram/webhook идёт через outbox:

```text
backend/logs/delivery_outbox.jsonl
```

Проверить outbox:

```bash
curl -s "http://localhost:8000/api/delivery/outbox?token=demo-operator-token"
```

Повторить due-доставки:

```bash
curl -s -X POST "http://localhost:8000/api/delivery/retry?token=demo-operator-token"
```

Webhook клиента указывается в `lead_webhook_url`. При отправке backend добавляет
headers `X-Delivery-ID` и `X-Company-ID`, чтобы принимающая сторона могла
дедуплицировать повторы.

## Аналитика

Простая сводка:

```bash
curl -s "http://localhost:8000/api/analytics/summary?token=demo-operator-token"
```

По одному клиенту:

```bash
curl -s "http://localhost:8000/api/analytics/summary?company_id=client_id&token=demo-operator-token"
```

Сейчас это lightweight-аналитика по in-memory sessions и jsonl-логам: лиды,
operator requests, unknown questions, medical handoffs. Это не BI и не dashboard.

## Production notes

Минимальная схема:

```text
client site -> widget.js -> https://api.your-domain.example -> FastAPI container
```

Что поменять перед реальным клиентом:

- заменить `OPERATOR_TOKEN`;
- настроить `ALLOWED_ORIGINS`;
- поставить реальные `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` или `lead_webhook_url`;
- подключить домен и HTTPS через nginx/reverse proxy;
- не коммитить `.env`, `backend/logs/*.jsonl`, `backend/data/clients/*`.

Пример nginx-конфига лежит в `deploy/nginx.chat-widget.example.conf`.

## Быстрая диагностика

Backend жив:

```bash
curl http://localhost:8000/health
```

Bootstrap клиента:

```bash
curl "http://localhost:8000/api/widget/bootstrap?company_id=rosh_demo"
```

Логи контейнера:

```bash
docker compose logs -f backend
```

Если виджет не появляется:

- проверить `data-company-id`;
- проверить, что bootstrap не возвращает `404`;
- проверить `ALLOWED_ORIGINS`;
- открыть browser console на сайте клиента.

Если лид не дошёл:

- проверить `backend/logs/leads.jsonl`;
- проверить `/api/delivery/outbox`;
- если статус `failed`, посмотреть `last_error`;
- после исправления webhook/Telegram запустить `/api/delivery/retry`.
