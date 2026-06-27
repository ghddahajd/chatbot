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
