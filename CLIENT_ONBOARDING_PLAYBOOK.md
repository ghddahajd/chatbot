# Client onboarding playbook

Рабочий порядок для подключения первого клиента. Это не V2/SaaS-процесс, а managed-service запуск: мы вручную заводим клиента, проверяем KB и отдаём embed script.

## 1. Что запросить у клиента

Минимальный набор:

- Название компании и город обслуживания.
- Сайт и рабочий домен, куда будет вставлен виджет.
- Telegram/WhatsApp/почта для заявок, если есть.
- Список услуг: название, короткое описание, цена, длительность если есть.
- Синонимы услуг: как клиенты обычно это называют.
- Частые вопросы: адрес, график, запись, оплата, гарантии, ограничения.
- Что бот не должен обещать: диагнозы, гарантии, точные сроки, нестандартные цены.
- Куда переводить сложные вопросы: оператор, Telegram, форма заявки.

Если данных мало, стартуем с 5-10 услугами и 3-5 FAQ. Потом докидываем по реальным диалогам.

## 2. Как выбрать domain_profile

`domain_profile` определяет, какие темы нельзя отдавать в свободный ответ.

Примеры:

```yaml
domain_profile:
  type: generic
  restricted_advice: []
```

Для медицины:

```yaml
domain_profile:
  type: medical
  restricted_advice:
    - medical
    - diagnosis
    - treatment
```

Для автосервиса обычно не надо включать medical-ограничения. Опасные темы лучше сначала покрывать FAQ/оператором, а не хардкодить в коде.

## 3. Черновик KB

Создать draft:

```bash
.venv/bin/python backend/scripts/create_kb_draft.py \
  --company-id client_id \
  --company-name "Название клиента" \
  --city "Город" \
  --phone "+7 000 000-00-00" \
  --website-url "https://client-site.example"
```

Заполнить:

```text
new/kb_drafts/client_id/company.yaml
new/kb_drafts/client_id/config.yaml
new/kb_drafts/client_id/services.json
new/kb_drafts/client_id/prices.json
new/kb_drafts/client_id/faq.md
```

Правило: цены и услуги только в structured KB. Не класть цены только в FAQ, иначе бот не должен считать их источником истины.

## 4. Dry-run перед публикацией

```bash
.venv/bin/python backend/scripts/onboard_client.py new/kb_drafts/client_id --dry-run
```

Блокеры надо исправить до публикации. Предупреждения можно оставить, если клиент запускается в тестовом режиме.

Публикация:

```bash
.venv/bin/python backend/scripts/onboard_client.py new/kb_drafts/client_id --publish
```

После публикации:

```bash
docker compose restart backend
```

## 5. Client-specific evals

Создать локальный файл:

```text
evals/clients/client_id.yaml
```

Он не коммитится. Туда кладём реальные фразы клиента и спорные кейсы.

Пример:

```yaml
scenarios:
  - message: "сколько стоит {first_service}"
    action: "answer"
    marker: "price"
  - message: "есть услуга которой нет"
    action: "clarify"
    marker: "unknown_service"
```

Проверка:

```bash
.venv/bin/python backend/scripts/smoke_ai_scenarios.py --company=client_id
```

В выводе должно быть видно:

```text
Scenario sets: universal (...), domain:..., client:client_id (...)
```

## 6. Launch checks

Перед отдачей клиенту:

```bash
.venv/bin/python backend/scripts/client_launch_check.py --company=client_id
.venv/bin/python backend/scripts/smoke_ai_scenarios.py --company=client_id
.venv/bin/python backend/scripts/smoke_managed.py
```

Минимум для запуска:

- `client_launch_check.py` зелёный.
- `smoke_ai_scenarios.py` без красных критичных сценариев.
- `bootstrap` работает с домена клиента.
- В ответах нет услуг/цен другого клиента.

## 7. Embed

Рекомендуемый вариант:

```html
<script
  src="https://your-domain/static/widget.js"
  data-company-id="client_id"
  data-api-base="https://your-domain"
  defer
></script>
```

Автодетект:

```html
<script
  src="https://your-domain/static/widget.js"
  data-api-base="https://your-domain"
  defer
></script>
```

Для автодетекта домен должен быть уникальным в `allowed_domains` среди всех клиентов.

## 8. Ручная проверка на сайте

Пройти прямо в виджете:

- `привет` → нормальный короткий ответ.
- `покажи услуги` → услуги этого клиента.
- `сколько стоит <реальная услуга>` → цена из `prices.json`.
- `есть <услуга которой нет>` → бот не выдумывает.
- `хочу записаться` → просит контактные данные.
- `Иван +7... хочу записаться` → лид сохраняется с `company_id`.
- `позовите оператора` → handoff работает.
- Перезагрузка страницы → история и статус сессии сохраняются.

Для restricted-доменов отдельно:

- Медицинский клиент: опасный/симптомный вопрос → оператор, без совета.
- Не medical клиент: медицинские слова не должны случайно ломать обычные услуги.

## 9. Если что-то пошло не так

- `bootstrap 404` — неверный `company_id` или клиент не опубликован.
- `bootstrap 403` — домен не в `allowed_domains` или не в `ALLOWED_ORIGINS`.
- Бот отвечает чужими услугами — проверить `company_id`, `data-company-id`, `CLIENTS_DATA_DIR`.
- Бот не видит услугу — добавить синонимы в `services.json`.
- Цена не находится — проверить `service_id` в `prices.json`.
- Handoff не работает — проверить operator panel и WebSocket.
- Telegram не приходит — проверить `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID`.

## 10. Первые 24 часа после запуска

- Смотреть `backend/logs/*.jsonl`.
- Собирать фразы, где бот уточнял или говорил “не нашёл”.
- Добавлять реальные фразы в `evals/clients/client_id.yaml`.
- Обновлять KB маленькими пачками, после каждой пачки прогонять smoke.
