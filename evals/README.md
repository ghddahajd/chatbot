# AI smoke evals

`smoke_ai_scenarios.py` собирает сценарии слоями:

1. `evals/universal.yaml` — базовые сценарии для любого клиента.
2. `evals/domains/<domain>.yaml` — доменные сценарии по `domain_profile`.
3. `evals/clients/<company_id>.yaml` — локальные реальные фразы клиента.

Client-specific evals не коммитятся: там могут быть реальные запросы и данные клиента.

Формат сценария:

```yaml
scenarios:
  - message: "сколько стоит {first_service}"
    action: "answer"
    marker: "price"
```

Доступные placeholders:

- `{first_service}` — первая услуга из KB клиента.
- `{second_service}` — вторая услуга из KB клиента.

Проверка:

```bash
.venv/bin/python backend/scripts/smoke_ai_scenarios.py --company=rosh_demo
.venv/bin/python backend/scripts/smoke_ai_scenarios.py --company=auto_service_demo
```
