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
new/kb_drafts/client_id/config.yaml    # features виджета, если нужен override
new/kb_drafts/client_id/services.json  # услуги, описания, синонимы
new/kb_drafts/client_id/prices.json    # цены с оговорками
new/kb_drafts/client_id/faq.md         # частые вопросы
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

## Troubleshooting

- Виджет не появляется → проверить `ALLOWED_ORIGINS` в `.env`.
- `bootstrap 403` → домен не добавлен в `allowed_domains` в `company.yaml`.
- `bootstrap 404` → `company_id` не совпадает с именем папки или клиент не опубликован.
- Бот говорит “не вижу в базе” → проверить `services.json` и синонимы услуги.
- Кнопка Telegram не работает → проверить `telegram_url` в `company.yaml`.
- Автодетект не работает → домен должен быть уникальным среди всех клиентов.
