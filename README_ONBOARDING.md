# Подключение нового клиента

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
- FAQ содержит хотя бы несколько частых вопросов.
- `website_url` и `telegram_url` заполнены, если нужны кнопки в чате.

Если есть только `⚠️` предупреждения, публикация разрешена, но качество ответов может быть ниже.

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
- Автодетект не работает → домен должен быть уникальным среди всех клиентов.

## Как подключаем клиента за 15 минут

1. Создать KB, примерно 5 минут:

```bash
cp -r backend/data/clients/_template backend/data/clients/{id}
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
