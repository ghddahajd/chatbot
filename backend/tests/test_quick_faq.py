"""проверки второго экрана "Частые вопросы" — bootstrap + /api/chat/faq-answer (2026-08-28)."""

import json
import shutil
from pathlib import Path

from app.models import MessageRole


def _copy_rosh_import_demo(test_client, managed_env) -> None:
    source_dir = Path("backend/data/clients/rosh_import_demo")
    target_dir = managed_env["clients_dir"] / "rosh_import_demo"
    if not target_dir.exists():
        shutil.copytree(source_dir, target_dir)
    test_client.app.state.knowledge_base_resolver._cache.clear()


def test_bootstrap_includes_quick_faq_for_rosh_import_demo(test_client, managed_env) -> None:
    _copy_rosh_import_demo(test_client, managed_env)

    response = test_client.get(
        "/api/widget/bootstrap?company_id=rosh_import_demo",
        headers={"origin": "http://localhost:5500"},
    )

    assert response.status_code == 200
    quick_faq = response.json()["quick_faq"]
    assert len(quick_faq) == 7
    ids = {item["id"] for item in quick_faq}
    assert "faq_lab_tests" in ids
    lab_tests_item = next(item for item in quick_faq if item["id"] == "faq_lab_tests")
    assert lab_tests_item["question"] == "Можно у вас сдать анализы?"
    assert lab_tests_item["answer"] == "Да, сдать анализы у нас можно. Точный список и как записаться подскажет менеджер."


def test_bootstrap_quick_faq_empty_when_no_file(test_client) -> None:
    """rosh_demo (стандартная тестовая фикстура) не имеет faq_quick_answers.json — не должно
    падать, просто пустой список."""

    response = test_client.get(
        "/api/widget/bootstrap?company_id=rosh_demo",
        headers={"origin": "http://localhost:5500"},
    )

    assert response.status_code == 200
    assert response.json()["quick_faq"] == []


def test_faq_answer_returns_canned_answer_and_persists_history(test_client, managed_env) -> None:
    _copy_rosh_import_demo(test_client, managed_env)

    response = test_client.post(
        "/api/chat/faq-answer",
        json={"session_id": "faq-test-1", "company_id": "rosh_import_demo", "faq_id": "faq_lab_tests"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["action"] == "answer"
    assert payload["answer"] == "Да, сдать анализы у нас можно. Точный список и как записаться подскажет менеджер."
    assert payload["quick_actions"] == []
    assert payload["lead_created"] is False

    session_response = test_client.get(f"/api/chat/session/{payload['session_id']}")
    assert session_response.status_code == 200
    messages = session_response.json()["messages"]
    assert len(messages) == 2
    assert messages[0]["role"] == MessageRole.USER.value
    assert messages[0]["text"] == "Можно у вас сдать анализы?"
    assert messages[1]["role"] == MessageRole.ASSISTANT.value
    assert messages[1]["text"] == "Да, сдать анализы у нас можно. Точный список и как записаться подскажет менеджер."


def test_faq_answer_unknown_faq_id_404(test_client, managed_env) -> None:
    _copy_rosh_import_demo(test_client, managed_env)

    response = test_client.post(
        "/api/chat/faq-answer",
        json={"session_id": "faq-test-2", "company_id": "rosh_import_demo", "faq_id": "not_a_real_id"},
    )

    assert response.status_code == 404


def test_faq_answer_unknown_company_404(test_client) -> None:
    response = test_client.post(
        "/api/chat/faq-answer",
        json={"session_id": "faq-test-3", "company_id": "no_such_company", "faq_id": "faq_lab_tests"},
    )

    assert response.status_code == 404


def test_faq_answer_tracks_message_answered_event(test_client, managed_env) -> None:
    """Живой сценарий, ради которого и добавили трекинг — иначе частота обращений к FAQ
    невидима для аналитики/логов, которыми пользователь уже гоняет проверки."""

    _copy_rosh_import_demo(test_client, managed_env)

    response = test_client.post(
        "/api/chat/faq-answer",
        json={"session_id": "faq-test-4", "company_id": "rosh_import_demo", "faq_id": "faq_lab_tests"},
    )
    assert response.status_code == 200

    analytics_file = managed_env["temp_dir"] / "analytics.jsonl"
    events = [json.loads(line) for line in analytics_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    faq_events = [e for e in events if e.get("event_type") == "message_answered" and e.get("session_id") == "faq-test-4"]
    assert len(faq_events) == 1
    assert faq_events[0]["metadata"]["policy_reason"] == "quick_faq"
    assert faq_events[0]["metadata"]["answer"] == "Да, сдать анализы у нас можно. Точный список и как записаться подскажет менеджер."
