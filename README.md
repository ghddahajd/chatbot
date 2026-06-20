# Chat Widget MVP

Тестовое MVP встраиваемого чат-виджета для медицинского / косметологического центра.

Идея такая:
- виджет подключается одной строкой через `<script>`
- бот отвечает только по локальной базе знаний
- по медицинским вопросам не импровизирует, а переводит на специалиста
- если нужен человек, чат можно передать оператору

Проект не про “умный универсальный AI-ассистент”, а про понятный и управляемый сценарий для сайта клиники.

## Что здесь уже есть

- backend на `FastAPI`
- knowledge base на `JSON / YAML`
- `policy guard`, который работает до LLM
- `MockLLM` по умолчанию
- возможность подключить `Gemini` или другой OpenAI-compatible API
- сбор лидов в `jsonl`
- operator panel
- `WebSocket`-handoff между клиентом и оператором
- demo widget на `Vanilla JS + Shadow DOM`
- восстановление состояния чата после перезагрузки страницы

## Стек

- frontend widget: `Vanilla JS`, `Shadow DOM`, `Custom Elements`
- backend: `Python`, `FastAPI`
- transport: `HTTP + WebSocket`
- knowledge base: `YAML / JSON`
- infra: `Docker Compose`

## Структура

```text
backend/
  app/
  data/
  logs/
widget/
demo/
docker-compose.yml
```

## Быстрый запуск

```bash
cp .env.example .env
docker compose up --build
```

После запуска:

- demo страница: `http://localhost:8000/demo/demo.html`
- operator panel: `http://localhost:8000/operator?token=demo-operator-token`
- healthcheck: `http://localhost:8000/health`

## Локальный запуск без Docker

```bash
cd backend
pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --port 8000
```

## Какой тут flow

У сессии есть 4 состояния:

```text
AI_ACTIVE -> WAITING_OPERATOR -> HUMAN_ACTIVE -> CLOSED
```

Что это значит:

- `AI_ACTIVE` — бот отвечает через `/api/chat/message`
- `WAITING_OPERATOR` — бот больше не отвечает, ждём подключения человека
- `HUMAN_ACTIVE` — оператор пишет напрямую в чат через `WebSocket`
- `CLOSED` — диалог завершён

Если пользователь перезагружает страницу, виджет восстанавливает текущую сессию и её состояние.

## Что бот не делает

- не выдумывает услуги, которых нет в базе
- не выдумывает цены и сроки
- не даёт медицинские рекомендации
- не ставит диагнозы
- не обещает результат

Если спрашивают то, что нельзя безопасно ответить через базу знаний, логика уводит чат либо в уточнение, либо в сценарий со специалистом.

## Какой LLM используется

По умолчанию проект работает вообще без внешнего API:

- `LLM_PROVIDER=mock`

То есть для demo можно запускать всё без ключей.

Если нужно протестировать на внешней модели, можно подключить Gemini.

Пример:

```env
LLM_PROVIDER=gemini
GEMINI_API_KEY=your_key
LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai
LLM_MODEL=gemini-3.5-flash
```

Либо можно использовать любой OpenAI-compatible endpoint:

```env
LLM_PROVIDER=openai_compatible
LLM_API_KEY=your_key
LLM_BASE_URL=https://your-endpoint.example/v1
LLM_MODEL=your-model-name
```

### Локальная модель (Ollama)

Если хочется гонять LLM локально без счёта за токены, можно подключить Ollama через OpenAI-compatible endpoint.

На macOS / Apple Silicon лучше запускать Ollama нативно на хосте, не внутри Docker, чтобы работало ускорение через Metal:

```bash
ollama pull qwen3:4b
ollama serve
```

В `.env` для backend:

```env
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://host.docker.internal:11434/v1
LLM_MODEL=qwen3:4b
LLM_API_KEY=local
LLM_SKIP_CLASSIFIER_FOR_LOCAL=true
```

Почему `host.docker.internal`: backend живёт внутри Docker-контейнера, и `localhost` внутри контейнера указывает не на macOS, а на сам контейнер. Этот адрес даёт контейнеру достучаться до Ollama на хосте.

JSON от небольших локальных моделей менее стабилен, чем от Claude/Gemini: они могут оборачивать ответ в Markdown или добавлять текст вокруг JSON. В коде есть tolerant parser и fallback, но для скорости локального режима классификатор LLM можно отключить через `LLM_SKIP_CLASSIFIER_FOR_LOCAL=true`. Тогда backend использует быстрый regex/policy слой и зовёт локальную модель только для ответа. Для показа клиенту я бы всё равно включал облачную модель. Локальная модель больше подходит для разработки и тестов без расходов.

## Что можно проверить руками

На demo-странице можно прогнать такие сценарии:

1. Спросить цену услуги из базы.
2. Спросить про длительность, которой нет в базе.
3. Задать медицинский вопрос.
4. Спросить услугу, которой нет в knowledge base.
5. Попросить оператора.
6. Оставить имя и телефон.
7. Открыть operator panel и взять чат.
8. Перезагрузить страницу и проверить, что состояние сессии восстановилось.

## Где лежат данные

- `backend/data/company.yaml`
- `backend/data/services.json`
- `backend/data/prices.json`
- `backend/data/faq.md`

Лиды пишутся сюда:

- `backend/logs/leads.jsonl`

## Что это за уровень готовности

Это именно MVP / тестовое, не production.

Что здесь сознательно не делалось:

- полноценный RAG
- CRM-интеграции
- автопарсинг всего сайта
- сложная админка
- тяжёлый frontend framework для виджета

## Что можно улучшать дальше

- допилить operator panel
- добавить smoke tests
- сделать более аккуратную demo-страницу
- улучшить мобильный UI виджета
- добавить TTL / retention-логику для сессий
- вынести хранилище сессий из памяти во что-то постоянное

## Почему так сделано

Я специально собирал проект без лишней сложности.

Здесь идея не в том, чтобы показать “супер-AI”, а в том, чтобы показать рабочий встраиваемый сценарий:

- controlled knowledge base
- policy-before-LLM
- handoff на оператора
- понятное поведение после reload
- возможность быстро поднять всё локально или через Docker
