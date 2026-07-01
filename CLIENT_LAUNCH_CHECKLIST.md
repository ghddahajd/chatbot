# Client Launch Checklist

Короткий чеклист перед подключением виджета на сайт клиента.
Подробный порядок подготовки KB: [README_ONBOARDING.md](README_ONBOARDING.md).

## 1. Данные клиента

- [ ] `company.yaml`: название, город, телефон, режим работы заполнены.
- [ ] `allowed_domains` содержит реальный домен клиента, не только `localhost`.
- [ ] `website_url` заполнен, если нужна кнопка перехода на сайт.
- [ ] `telegram_url` заполнен, если нужна кнопка Telegram.
- [ ] `config.yaml`: заголовок, подзаголовок, цвет и позиция виджета настроены.
- [ ] `services.json`: услуги названы так, как их называет клиент.
- [ ] У каждой услуги есть синонимы: короткие фразы, ошибки, разговорные варианты.
- [ ] `prices.json`: цены совпадают с `service_id` из `services.json`.
- [ ] `faq.md`: закрыты запись, отмена, оплата, адрес/выезд, что входит в услугу.

## 2. Проверка KB

```bash
python backend/scripts/onboard_client.py {company_id} --dry-run
python backend/scripts/validate_kb.py backend/data/clients/{company_id}
python backend/scripts/client_launch_check.py --company={company_id}
python backend/scripts/smoke_ai_scenarios.py --company={company_id}
```

Перед запуском не должно быть `❌ БЛОКЕР`. Предупреждения допустимы только если
они осознанные: например, Telegram или webhook ещё не подключены.

## 3. Безопасность и изоляция

- [ ] В production `.env` выставлено `DEV_MODE=false`.
- [ ] В `ALLOWED_ORIGINS` добавлены только домены, с которых виджет может делать запросы.
- [ ] `OPERATOR_TOKEN` заменён с demo-значения на длинный случайный токен.
- [ ] `TELEGRAM_BOT_TOKEN` хранится только в `.env`, не в клиентском YAML.
- [ ] Реальные KB клиентов не коммитятся в git.
- [ ] Проверено: неверный `company_id` возвращает `404`.
- [ ] Проверено: чужой `Origin` возвращает `403`.
- [ ] Проверено: виджет не запускается на данных другого клиента.
- [ ] Если используется автодетект без `data-company-id`, домен уникален среди клиентов.

Проверка bootstrap:

```bash
curl -i "https://api.example.com/api/widget/bootstrap?company_id={company_id}"
```

Проверка чужого домена:

```bash
curl -i \
  -H "Origin: https://wrong-domain.example" \
  "https://api.example.com/api/widget/bootstrap?company_id={company_id}"
```

Ожидаемый ответ для чужого домена: `403 Domain not allowed`.

## 4. Embed для интегратора

Рекомендуемый вариант:

```html
<script
  src="https://api.example.com/static/widget.js"
  data-company-id="{company_id}"
  data-api-base="https://api.example.com"
  defer
></script>
```

Автодетект без `data-company-id` использовать только если домен клиента уже
прописан в `allowed_domains` и не повторяется у других клиентов.

```html
<script
  src="https://api.example.com/static/widget.js"
  data-api-base="https://api.example.com"
  defer
></script>
```

Интегратору достаточно вставить script перед `</body>` и проверить, что виджет
появился после загрузки страницы.

## 5. Ручная проверка на сайте

- [ ] Виджет открывается и показывает название/тему клиента.
- [ ] `покажи услуги` возвращает услуги этого клиента.
- [ ] Цена реальной услуги берётся из KB и содержит оговорку.
- [ ] Несуществующая услуга не выдумывается.
- [ ] Короткая фраза после услуги работает по контексту: `цена`, `а сколько?`.
- [ ] Пользователь может оставить телефон в свободной форме: `89991234567 Алексей`.
- [ ] Лид сохраняется с правильным `company_id`.
- [ ] Запрос оператора переводит сессию в `WAITING_OPERATOR`.
- [ ] Оператор может взять чат и ответить через panel.
- [ ] Кнопки сайта/Telegram скрываются или работают корректно.

## 6. Уведомления

Проверить destinations без отправки:

```bash
python backend/scripts/send_test_delivery.py \
  --company={company_id} \
  --event=all \
  --dry-run
```

Проверить реальную доставку:

```bash
python backend/scripts/send_test_delivery.py \
  --company={company_id} \
  --event=all
```

Если доставка упала:

```bash
tail -n 20 backend/logs/delivery_outbox.jsonl
curl -s -X POST "https://api.example.com/api/delivery/retry?token={OPERATOR_TOKEN}"
```

## 7. После запуска

- [ ] Первые сутки проверить `backend/logs/` на ошибки delivery и лидов.
- [ ] Собрать реальные непонятные фразы пользователей в eval/debug-набор.
- [ ] Пополнить синонимы услуг, если пользователи называют услуги иначе.
- [ ] Обновить FAQ, если бот часто отвечает “нет в базе”.
- [ ] Не менять policy/LLM под одного клиента без regression smoke по остальным клиентам.
