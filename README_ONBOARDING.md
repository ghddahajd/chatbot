# Подключение нового клиента

Практический порядок запуска с вопросами клиенту и ручными проверками:
[CLIENT_ONBOARDING_PLAYBOOK.md](CLIENT_ONBOARDING_PLAYBOOK.md).

## Быстрый старт

1. Создать draft из шаблона:

```bash
python3 backend/scripts/create_kb_draft.py \
  --company-id client_id \
  --company-name "Название клиента" \
  --city "Москва" \
  --phone "+7 000 000-00-00" \
  --website-url "https://client-site.example"
```

2. Заполнить файлы draft:

```text
new/kb_drafts/client_id/company.yaml   # контакты, allowed_domains, ссылки
new/kb_drafts/client_id/config.yaml    # features и внешний вид виджета
new/kb_drafts/client_id/services.json  # услуги, описания, синонимы
new/kb_drafts/client_id/prices.json    # цены с оговорками
new/kb_drafts/client_id/faq.md         # частые вопросы
```

Пример `config.yaml`:

```yaml
features:
  operator: true
  lead_capture: true
  analytics: false
widget:
  primary_color: "#1F7A5C"
  button_color: "#1F7A5C"
  header_title: "Чат с поддержкой"
  header_subtitle: "Подскажем по услугам и ценам"
  position: "bottom-right" # bottom-right / bottom-left
  avatar_emoji: "💬"
```

3. Проверить без публикации:

```bash
python3 backend/scripts/onboard_client.py client_id --dry-run
```

4. Опубликовать:

```bash
python3 backend/scripts/onboard_client.py client_id --publish
```

5. Добавить домен клиента в `ALLOWED_ORIGINS` в `.env`.

6. Вставить на сайт клиента.

Явный вариант, рекомендуемый:

```html
<script
  src="https://your-domain/static/widget.js"
  data-company-id="client_id"
  data-api-base="https://your-domain"
  defer
></script>
```

Автодетект, если домен есть в `allowed_domains`:

```html
<script
  src="https://your-domain/static/widget.js"
  data-api-base="https://your-domain"
  defer
></script>
```

## Чеклист готовности

Запустить перед запуском клиента:

```bash
python3 backend/scripts/onboard_client.py client_id --dry-run
```

Что важно увидеть:

- Нет строк `❌ БЛОКЕР`.
- `company_name`, `city`, `allowed_domains` заполнены.
- Услуги и цены совпадают по `service_id`.
- У всех услуг есть синонимы.
- FAQ не пустой и содержит минимум 3 секции `## ...`.
- `website_url` и `telegram_url` заполнены, если нужны кнопки в чате.

Если есть только `⚠️` предупреждения, публикация разрешена, но качество ответов может быть ниже.

## Типичные вопросы которые надо закрыть в FAQ

Универсальный минимум для любого клиента:

- Запись и отмена: как записаться, как перенести визит, за сколько времени отменять.
- Как добраться / парковка: адрес, ориентиры, карта, вход, парковка.
- Оплата: карта, наличные, счёт, аванс, рассрочка, возвраты.
- Что взять с собой: документы, данные, материалы, подготовка.
- Частые вопросы по услугам: сроки, ограничения, что входит и что не входит.

Medical / beauty:

- Можно ли получить точную цену до консультации.
- Нужна ли подготовка перед процедурой или приёмом.
- Когда вопрос надо передать специалисту, а не отвечать в чате.
- Какие документы или анализы взять, если это применимо.

Auto:

- Можно ли приехать без записи.
- Можно ли привезти свои запчасти.
- Сколько занимает диагностика или типовая работа.
- От чего зависит итоговая цена после осмотра.

Legal:

- Какие документы нужны для первичной консультации.
- Можно ли оценить перспективы дела по телефону.
- Как проходит оплата: консультация, договор, этапы.
- Какие вопросы нельзя решать без изучения документов.

## Проверка после публикации

```bash
python3 backend/scripts/validate_kb.py backend/data/clients/client_id
python3 backend/scripts/simulate_kb.py backend/data/clients/client_id
python3 backend/scripts/smoke_onboarding.py
```

После изменения клиентской KB перезапустить backend:

```bash
docker compose restart backend
```

## Настройка уведомлений

Заявки и запросы оператора доставляются через generic delivery outbox.
Даже если Telegram или webhook временно недоступны, событие пишется в
`backend/logs/delivery_outbox.jsonl` и может быть повторено позже.

Токен Telegram хранится только в `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:bot-token
```

В `config.yaml` клиента указывается только `chat_id` и список событий:

```yaml
notifications:
  telegram:
    enabled: true
    chat_id: "-100123456789"
    events:
      - lead_created
      - booking_created
      - operator_requested
  webhook:
    enabled: false
    url: ""
    secret: ""
    events:
      - lead_created
      - booking_created
      - operator_requested
```

Webhook включается так:

```yaml
notifications:
  webhook:
    enabled: true
    url: "https://client-crm.example/webhook"
    secret: ""
    events:
      - lead_created
      - booking_created
      - operator_requested
```

Backend отправляет webhook с заголовками:

```text
X-Widget-Event: booking_created
X-Delivery-ID: <stable uuid>
X-Company-ID: <company_id>
```

`X-Delivery-ID` одинаковый при retry, чтобы CRM могла дедуплицировать
повторные доставки.

## Проверка уведомлений

Перед запуском клиента проверь delivery без ручного чата:

```bash
python3 backend/scripts/send_test_delivery.py \
  --company=client_id \
  --event=booking_created \
  --dry-run
```

`--dry-run` показывает destinations и payload, но ничего не отправляет и
не пишет в outbox.

Реальная тестовая отправка:

```bash
python3 backend/scripts/send_test_delivery.py \
  --company=client_id \
  --event=all
```

Ожидаемые статусы:

- `sent (status 200)` — канал принял событие.
- `not configured` — канал выключен или не заполнен в `config.yaml` / `.env`.
- `failed — ConnectError` — backend не смог подключиться к webhook URL.
- `failed — http_status_...` — webhook ответил ошибкой.

Проверить outbox:

```bash
tail -n 20 backend/logs/delivery_outbox.jsonl
```

Повторить due-доставки:

```bash
curl -s -X POST \
  "http://localhost:8000/api/delivery/retry?token=demo-operator-token"
```

## Проверка вставки виджета

Перед передачей клиенту стоит руками проверить четыре сценария:

- Правильный `data-company-id` → виджет открывается и отвечает по базе клиента.
- Неверный `data-company-id` → виджет показывает `клиент не найден`.
- Домен не добавлен в `allowed_domains` → виджет показывает `домен не разрешён`.
- Backend выключен или недоступен → виджет показывает, что сервис временно недоступен.

Если встраивание идёт через автодетект без `data-company-id`, домен должен быть
уникальным среди всех клиентов. При дубле виджет покажет ошибку про несколько
клиентов на одном домене.

## Troubleshooting

- Виджет не появляется → проверить `ALLOWED_ORIGINS` в `.env`.
- `bootstrap 403` → домен не добавлен в `allowed_domains` в `company.yaml`.
- `bootstrap 404` → `company_id` не совпадает с именем папки или клиент не опубликован.
- Бот говорит “не вижу в базе” → проверить `services.json` и синонимы услуги.
- Кнопка Telegram не работает → проверить `telegram_url` в `company.yaml`.
- Telegram-уведомления не приходят → проверить `TELEGRAM_BOT_TOKEN` в `.env`
  и `notifications.telegram.chat_id` в `config.yaml`.
- Webhook падает с `ConnectError` → проверить URL, доступность CRM и HTTPS.
- В outbox нет записей → для события нет включённых destinations.
- Автодетект не работает → домен должен быть уникальным среди всех клиентов.

## Как подключаем клиента за 15 минут

1. Создать KB, примерно 5 минут:

```bash
python3 backend/scripts/create_kb_draft.py \
  --company-id {id} \
  --company-name "Название клиента" \
  --city "Москва" \
  --phone "+7 000 000-00-00"
```

Или вручную скопировать нейтральный шаблон:

```bash
cp -r backend/data/client_template/universal_sample new/kb_drafts/{id}
```

Заполнить `company.yaml`, `config.yaml`, `services.json`, `prices.json`, `faq.md`.

2. Проверить, примерно 2 минуты:

```bash
python3 backend/scripts/onboard_client.py {id} --dry-run
python3 backend/scripts/client_launch_check.py --company={id}
```

3. Опубликовать и перезапустить, примерно 1 минута:

```bash
python3 backend/scripts/onboard_client.py {id} --publish
docker compose restart backend
```

4. Добавить домен клиента в `.env` → `ALLOWED_ORIGINS`, примерно 1 минута:

```bash
docker compose restart backend
```

5. Вставить на сайт, примерно 1 минута:

```html
<script
  src="https://your-domain/static/widget.js"
  data-company-id="{id}"
  defer
></script>
```

6. Финально пройти [CLIENT_LAUNCH_CHECKLIST.md](CLIENT_LAUNCH_CHECKLIST.md), примерно 5 минут.

Итого: клиент работает через 15 минут после получения материалов.
