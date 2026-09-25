"""служебный маршрут POST /api/leads: заявки посетителей создаёт сам чат, этот — только с токеном,
иначе кто угодно заливал бы фейковые заявки."""

from __future__ import annotations

import json

LEAD = {"company_id": "rosh_demo", "session_id": "check", "name": "Тест", "phone": "+7 900 000-00-00", "summary": "Проверка"}


def test_without_token_the_lead_is_rejected_and_not_saved(test_client, managed_env) -> None:
    response = test_client.post("/api/leads", json=LEAD)

    assert response.status_code == 403
    assert not (managed_env["temp_dir"] / "leads.jsonl").exists()


def test_with_token_the_lead_is_saved(test_client, managed_env) -> None:
    response = test_client.post("/api/leads", json=LEAD, headers={"x-operator-token": "demo-operator-token"})

    assert response.status_code == 200
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["company_id"] == "rosh_demo"


def test_oversized_fields_are_rejected(test_client) -> None:
    response = test_client.post(
        "/api/leads", json={**LEAD, "summary": "х" * 5000}, headers={"x-operator-token": "demo-operator-token"}
    )

    assert response.status_code == 422
