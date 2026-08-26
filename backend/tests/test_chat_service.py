"""интеграционные проверки chat message flow."""

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from app.llm.mock import MockLLMClient
from app.models import Message, MessageRole, Session
from app.services.chat_service import ChatService


def _copy_rosh_import_demo_for_chat_test(test_client, managed_env) -> None:
    source_dir = Path("backend/data/clients/rosh_import_demo")
    target_dir = managed_env["clients_dir"] / "rosh_import_demo"
    if not target_dir.exists():
        shutil.copytree(source_dir, target_dir)
    test_client.app.state.knowledge_base_resolver._cache.clear()


def _quick_action_labels(payload: dict) -> list[str]:
    return [str(action.get("label") or "") for action in payload.get("quick_actions", [])]


def _post_chat(test_client, *, message: str, session_id: str | None = None, company_id: str = "rosh_demo") -> dict:
    response = test_client.post(
        "/api/chat/message",
        json={"company_id": company_id, "session_id": session_id, "message": message},
    )
    assert response.status_code == 200
    return response.json()


class _EchoSummaryLLMClient(MockLLMClient):
    async def summarize_session(self, session, lead):
        return " | ".join(
            str(message.text or "")
            for message in session.messages
            if message.role.value == "user" and str(message.text or "").strip()
        )


class _FailingSummaryLLMClient(MockLLMClient):
    async def summarize_session(self, session, lead):
        raise RuntimeError("summarizer unavailable")


class _ArticleGuidanceLLMClient(MockLLMClient):
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.last_context = None

    async def complete(self, system_prompt, context, user_message, history):
        if context.get("question_type") == "article_guidance_excerpt":
            self.last_context = context
            return self.answer
        return await super().complete(system_prompt, context, user_message, history)


def _add_article_excerpt_for_chat_test(test_client, managed_env, excerpt: str) -> None:
    _copy_rosh_import_demo_for_chat_test(test_client, managed_env)
    map_path = managed_env["clients_dir"] / "rosh_import_demo" / "article_service_map.yaml"
    text = map_path.read_text(encoding="utf-8")
    text = text.replace(
        "  title: Расширенные поры на лице (блог)\n",
        f"  title: Расширенные поры на лице (блог)\n  excerpt: {excerpt}\n",
        1,
    )
    map_path.write_text(text, encoding="utf-8")
    test_client.app.state.knowledge_base_resolver._cache.clear()


def test_cosmetic_multi_candidate_followup_reoffers_same_services(test_client, managed_env) -> None:
    """B4-раздел аудита: 'хочу убрать морщины вокруг глаз' резолвится в НЕСКОЛЬКО кандидатов
    (Ботулинотерапия/Биоревитализация/Филлеры) — угадывать один нельзя, но follow-up 'а
    сколько это стоит?' раньше проваливался в общий 'не нашёл подтверждения', потому что
    (а) ни один service_id не был запомнён как контекст и (б) даже с контекстом анафора
    'это' ломала consecutive-token матчинг фразы 'сколько стоит'. Оба фиксятся здесь."""

    _copy_rosh_import_demo_for_chat_test(test_client, managed_env)
    test_client.app.state.llm_client = _ArticleGuidanceLLMClient("Рекомендую вам Филлеры, это вам подходит.")

    first_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        message="здравствуйте, хочу убрать морщины вокруг глаз",
    )
    assert first_payload["action"] == "answer"
    first_labels = _quick_action_labels(first_payload)
    assert {"Ботулинотерапия", "Биоревитализация", "Филлеры"} <= set(first_labels)

    followup_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        session_id=first_payload["session_id"],
        message="а сколько это стоит?",
    )

    assert followup_payload["action"] == "clarify"
    assert "не нашёл подтверждения" not in followup_payload["answer"]
    for service_name in ("Ботулинотерапия", "Биоревитализация", "Филлеры"):
        assert service_name in followup_payload["answer"]
    followup_labels = _quick_action_labels(followup_payload)
    assert {"Ботулинотерапия", "Биоревитализация", "Филлеры"} <= set(followup_labels)


def test_contact_prompt_stays_ai_active_and_can_be_cancelled(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "Хочу оставить телефон"},
    )
    first_payload = first_response.json()

    assert first_response.status_code == 200
    assert first_payload["action"] == "ask_contact"
    assert first_payload["status"] == "AI_ACTIVE"
    assert "телефон" in first_payload["answer"].lower()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "нет",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "clarify"
    assert second_payload["status"] == "AI_ACTIVE"
    assert "контакт пока не берём" in second_payload["answer"].lower()


def test_booking_prompt_can_be_cancelled_with_common_typo(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу заптсаьбся на чистку лица"},
    )
    first_payload = first_response.json()

    assert first_response.status_code == 200
    assert first_payload["action"] == "clarify"
    assert "телефон" in first_payload["answer"].lower()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "омтена",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "clarify"
    assert "заявку пока не передаю" in second_payload["answer"].lower()
    assert second_payload["lead_created"] is False


def test_booking_prompt_accepts_service_name_as_next_step(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "запишите меня"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "на чистку лица",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "clarify"
    assert "телефон" in second_payload["answer"].lower()
    assert "такой услуги" not in second_payload["answer"].lower()

    third_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +79991234567",
        },
    )
    third_payload = third_response.json()

    assert third_response.status_code == 200
    assert third_payload["lead_created"] is True


def test_earlier_complaint_flag_survives_into_later_lead(test_client, managed_env) -> None:
    """Жалоба, поданная в начале разговора, не должна потеряться, если человек потом
    продолжил и оставил контакт по совсем другому поводу — оператор должен это увидеть."""

    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "у меня жалоба на администратора"},
    )
    first_payload = first_response.json()
    session_id = first_payload["session_id"]

    second_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "запишите меня на чистку лица"},
    )
    second_payload = second_response.json()

    third_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "Иван +79991234567"},
    )
    third_payload = third_response.json()
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])

    assert second_response.status_code == 200
    assert third_response.status_code == 200
    assert third_payload["lead_created"] is True
    assert "жалоба" in lead["summary"].lower()


def test_booking_time_preference_is_captured_and_surfaced_in_lead(test_client, managed_env) -> None:
    """§2/§3.3 скрипта: assumptive close "утро или вечер?" — квик-экшены на booking_contact_prompt.
    Клик по "Утром" должен подтверждаться отдельно и попасть в текст заявки менеджеру, не
    потеряться молча в общем "напишите телефон"."""

    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "запишите меня на чистку лица"},
    )
    first_payload = first_response.json()
    assert "Утром" in first_payload["quick_actions"] or any(
        isinstance(item, dict) and item.get("label") == "Утром" for item in first_payload["quick_actions"]
    )

    second_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": first_payload["session_id"], "message": "Утром"},
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "clarify"
    assert "утром" in second_payload["answer"].lower()
    assert "имя и телефон" in second_payload["answer"].lower()

    third_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +79991234567",
        },
    )
    third_payload = third_response.json()
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])

    assert third_response.status_code == 200
    assert third_payload["lead_created"] is True
    assert "утром" in lead["summary"].lower()


def test_article_guidance_uses_llm_when_approved_excerpt_passes_validator(test_client, managed_env) -> None:
    _add_article_excerpt_for_chat_test(
        test_client,
        managed_env,
        "Расширенные поры могут быть связаны с особенностями кожи и требуют индивидуального подбора.",
    )
    llm_client = _ArticleGuidanceLLMClient(
        (
            "В материалах центра указано, что расширенные поры требуют индивидуального подбора. "
            "С этой темой связаны Чистки, Пилинги и Лазерный пилинг. "
            "Точный подбор подтвердит специалист на консультации."
        )
    )
    test_client.app.state.llm_client = llm_client

    payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        message="расширенные поры что делать",
    )

    assert payload["action"] == "answer"
    assert "В материалах центра указано" in payload["answer"]
    assert "По теме «Расширенные поры на лице (блог)»" not in payload["answer"]
    assert llm_client.last_context["question_type"] == "article_guidance_excerpt"
    assert "message_to_user" not in llm_client.last_context


def test_article_guidance_falls_back_when_llm_recommends_service(test_client, managed_env) -> None:
    _add_article_excerpt_for_chat_test(
        test_client,
        managed_env,
        "Расширенные поры могут быть связаны с особенностями кожи и требуют индивидуального подбора.",
    )
    test_client.app.state.llm_client = _ArticleGuidanceLLMClient(
        "Рекомендую вам Филлеры, это вам подходит."
    )

    payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        message="расширенные поры что делать",
    )

    assert payload["action"] == "answer"
    assert "Рекомендую" not in payload["answer"]
    assert "По теме «Расширенные поры на лице (блог)»" in payload["answer"]


def test_contact_prompt_still_allows_new_price_question(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу оставить телефон"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "а сколько стоит чистка лица",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "answer"
    assert "стоимость" in second_payload["answer"].lower() or "₽" in second_payload["answer"]


def test_cancel_after_price_booking_compound_does_not_become_unknown_service(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "сколько стоит чистка лица и можно записаться",
        },
    )
    first_payload = first_response.json()

    assert first_response.status_code == 200
    assert first_payload["action"] == "answer"

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "отмена",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "clarify"
    assert "ничего пока не делаем" in second_payload["answer"].lower()
    assert "такой услуги" not in second_payload["answer"].lower()
    assert second_payload["lead_created"] is False


def test_price_followup_after_variant_reuses_last_variant(managed_env) -> None:
    source_dir = Path("backend/data/clients/rosh_import_demo")
    target_dir = managed_env["clients_dir"] / "rosh_import_demo"
    shutil.copytree(source_dir, target_dir)

    from app.main import app

    with TestClient(app) as test_client:
        first_response = test_client.post(
            "/api/chat/message",
            json={"company_id": "rosh_import_demo", "session_id": None, "message": "лазерная эпиляция"},
        )
        first_payload = first_response.json()

        second_response = test_client.post(
            "/api/chat/message",
            json={
                "company_id": "rosh_import_demo",
                "session_id": first_payload["session_id"],
                "message": "а на ногах?",
            },
        )
        second_payload = second_response.json()

        third_response = test_client.post(
            "/api/chat/message",
            json={
                "company_id": "rosh_import_demo",
                "session_id": first_payload["session_id"],
                "message": "а по деньгам?",
            },
        )
        third_payload = third_response.json()

    assert second_response.status_code == 200
    assert third_response.status_code == 200
    assert "ноги полностью" in second_payload["answer"].lower()
    assert "21 600" in second_payload["answer"]
    assert "ноги полностью" in third_payload["answer"].lower()
    assert "21 600" in third_payload["answer"]
    assert "от 1 800 до 24 000" not in third_payload["answer"]


def test_chat_rate_limit_blocks_single_ip_but_not_other_ips(managed_env, monkeypatch) -> None:
    monkeypatch.setenv("CHAT_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "2")

    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    with TestClient(app) as client:
        headers = {"X-Forwarded-For": "203.0.113.10"}
        first_response = client.post(
            "/api/chat/message",
            json={"company_id": "rosh_demo", "session_id": None, "message": "привет"},
            headers=headers,
        )
        second_response = client.post(
            "/api/chat/message",
            json={"company_id": "rosh_demo", "session_id": first_response.json()["session_id"], "message": "услуги"},
            headers=headers,
        )
        limited_response = client.post(
            "/api/chat/message",
            json={"company_id": "rosh_demo", "session_id": first_response.json()["session_id"], "message": "цены"},
            headers=headers,
        )
        other_ip_response = client.post(
            "/api/chat/message",
            json={"company_id": "rosh_demo", "session_id": None, "message": "привет"},
            headers={"X-Forwarded-For": "203.0.113.11"},
        )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert limited_response.status_code == 429
    assert other_ip_response.status_code == 200
    get_settings.cache_clear()


def test_chat_rate_limit_survives_client_rotating_x_forwarded_for(managed_env, monkeypatch) -> None:
    """Раньше client_ip брал ПЕРВОЕ (клиентское) значение X-Forwarded-For — атакующий менял
    заголовок на каждый запрос и получал новый rate-limit "ключ" каждый раз, лимит не работал
    вообще. С учётом доверенного прокси (Render = 1 hop) нужно брать значение С КОНЦА — то,
    что дописал НАШ прокси, а не то, что вписал клиент."""

    monkeypatch.setenv("CHAT_RATE_LIMIT_ENABLED", "true")
    monkeypatch.setenv("CHAT_RATE_LIMIT_PER_MINUTE", "2")
    monkeypatch.setenv("TRUSTED_PROXY_COUNT", "1")

    from app.config import get_settings
    from app.main import app

    get_settings.cache_clear()
    with TestClient(app) as client:
        statuses = []
        for i in range(5):
            # Каждый запрос выглядит как будто пришёл с нашего прокси (последнее значение —
            # то, что дописал доверенный hop) от одного и того же реального клиента, но с
            # РАЗНЫМ поддельным клиентским префиксом — именно так атака и выглядела в аудите.
            headers = {"X-Forwarded-For": f"1.2.3.{i}, 203.0.113.10"}
            response = client.post(
                "/api/chat/message",
                json={"company_id": "rosh_demo", "session_id": None, "message": "привет"},
                headers=headers,
            )
            statuses.append(response.status_code)

    assert statuses == [200, 200, 429, 429, 429]
    get_settings.cache_clear()


def test_message_count_limit_closes_session_for_real_reset_button(test_client) -> None:
    from app.models import SessionStatus
    from app.routes.chat_utils import MAX_SESSION_MESSAGES

    session_id = None
    for index in range(MAX_SESSION_MESSAGES):
        payload = _post_chat(test_client, message=f"привет {index}", session_id=session_id)
        session_id = payload["session_id"]

    limited_payload = _post_chat(test_client, message="ещё вопрос", session_id=session_id)
    stored_session = test_client.app.state.session_store._sessions[session_id]

    assert limited_payload["status"] == SessionStatus.CLOSED.value
    assert limited_payload["quick_actions"] == []
    assert stored_session.status == SessionStatus.CLOSED


def test_booking_contact_enqueues_booking_event(test_client, monkeypatch) -> None:
    events = []

    async def fake_enqueue_event(**kwargs):
        events.append(kwargs)
        return []

    monkeypatch.setattr(test_client.app.state.delivery_service, "enqueue_event", fake_enqueue_event)

    first_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "хочу записаться на Консультация косметолога",
        },
    )
    first_payload = first_response.json()

    assert first_response.status_code == 200
    assert first_payload["action"] == "clarify"

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +7 999 123-45-67",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["lead_created"] is True
    assert events[-1]["event_type"] == "booking_created"
    assert events[-1]["company_id"] == "rosh_demo"
    assert events[-1]["session_id"] == first_payload["session_id"]


def test_direct_address_question_without_question_mark_answered_not_treated_as_contact(
    test_client,
) -> None:
    """Живой репро (аудит §2026-08-22, F-10): во время сбора контакта (pending_action=
    BOOKING_CONTACT) пользователь пишет "стоп я же не спросила про заявку ещё. скажи адрес
    сначала" — БЕЗ "?". _looks_like_new_question раньше матчила смену темы только по "?",
    это сообщение проваливалось в общий "напишите телефон" вместо ответа на прямой вопрос
    об адресе."""

    first_payload = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "хочу записаться на Консультация косметолога",
        },
    ).json()
    assert first_payload["action"] == "clarify"

    second_payload = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "стоп я же не спросила про заявку ещё. скажи адрес сначала",
        },
    ).json()

    assert "напишите" not in second_payload["answer"].lower()
    assert "телефон" not in second_payload["answer"].lower()


def test_successful_booking_clears_pending_and_does_not_duplicate_lead(test_client, managed_env) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "хочу записаться на Консультация косметолога",
        },
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +7 999 123-45-67",
        },
    )
    second_payload = second_response.json()
    assert second_response.status_code == 200
    assert second_payload["lead_created"] is True

    leads_file = managed_env["temp_dir"] / "leads.jsonl"
    leads_before = leads_file.read_text(encoding="utf-8").splitlines()

    third_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "+7 999 123-45-67",
        },
    )
    third_payload = third_response.json()
    leads_after = leads_file.read_text(encoding="utf-8").splitlines()

    assert third_response.status_code == 200
    assert third_payload["lead_created"] is False
    assert len(leads_after) == len(leads_before)


def test_regulated_question_soft_offers_without_operator_event(test_client, monkeypatch) -> None:
    events = []

    async def fake_enqueue_event(**kwargs):
        events.append(kwargs)
        return []

    monkeypatch.setattr(test_client.app.state.delivery_service, "enqueue_event", fake_enqueue_event)

    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "у меня воспаление что делать",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "clarify"
    assert payload["status"] == "AI_ACTIVE"
    # Топ-1, второй слой (аудит §2026-08-22): "воспаление" само по себе не входит в 4
    # острые категории раздела 5 (сильная боль/кровотечение/аллергия/резкое ухудшение) —
    # calm-тон, без 103. Тест был про механизм soft-offer-без-operator-event, не про тон.
    assert "103" not in payload["answer"]
    assert [action["label"] for action in payload["quick_actions"]] == [
        "Оставить телефон",
        "Подключить менеджера",
    ]
    assert events == []


def test_benign_pain_question_soft_offer_has_no_emergency_number(test_client) -> None:
    """B4-раздел аудита: 'а больно?' — обычный бытовой вопрос перед процедурой, не экстренная
    ситуация. Раньше вёл в тот же soft-offer текст, что и реально острые сигналы, с
    'если срочно — звоните... в скорую (103)' в любом случае. Пугает и выглядит сломанным."""

    payload = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "а больно?"},
    ).json()

    assert payload["action"] == "clarify"
    assert "103" not in payload["answer"]


def test_panic_message_gets_empathetic_prefix_before_medical_referral(test_client) -> None:
    """Живой репро (аудит §2026-08-22, "Ниже", находка A4): "я в ПАНИКЕ", "боюсь идти к
    врачу вообще" и т.д. — 16/16 живых проб получали один и тот же холодный процедурный
    текст без единого элемента эмоционального отклика. PAIN_FEAR_ANTICIPATION_KEYWORDS уже
    существовал для другой цели (объекшен-хендлинг) — переиспользован здесь для эмпатичного
    префикса, не заведён отдельный список специально под тон."""

    payload = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "я в панике, у меня родинка меняет цвет",
        },
    ).json()

    assert payload["action"] == "clarify"
    assert payload["answer"].startswith("Понимаю, это тревожит.")


def test_phone_given_alongside_regulated_question_still_creates_lead(test_client, monkeypatch) -> None:
    """Живой репро (аудит §2026-08-22, F-11): "окей ладно дам телефон. 89991234567. кстати я
    беременна это важно для приёма дерматолога?" — телефон и медицинский вопрос в ОДНОМ
    сообщении. medical_requested-ветка раньше возвращалась раньше общей "if phone:" проверки
    и никогда её не достигала — номер молча терялся, lead_created оставался False, хотя
    пациент прямым текстом дал контакт."""

    events = []

    async def fake_enqueue_event(**kwargs):
        events.append(kwargs)
        return []

    monkeypatch.setattr(test_client.app.state.delivery_service, "enqueue_event", fake_enqueue_event)

    payload = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "окей ладно дам телефон. 89991234567. кстати я беременна это важно для приёма дерматолога?",
        },
    ).json()

    assert payload["lead_created"] is True
    assert events[-1]["event_type"] == "lead_created"
    assert events[-1]["company_id"] == "rosh_demo"
    # Мягкий медицинский текст должен остаться, не замениться на generic "спасибо, передали
    # контакты" — это по-прежнему TRANSFER_OPERATOR/REGULATED_ADVICE ветка, просто с лидом.
    assert "специалист" in payload["answer"].lower()
    assert "скорую" not in payload["answer"]


def test_llm_consultation_risk_path_uses_computed_urgency_not_default_true(test_client, monkeypatch) -> None:
    """Живой репро (аудит §2026-08-22, "скорая 103" систематически): _regulated_soft_offer_
    response() раньше вызывался с LLM-риск-пути (classify_consultation_risk == RESTRICTED)
    БЕЗ urgent= вообще — молча получал дефолт True на любое сообщение, попавшее именно в этот
    путь, независимо от текста. Форсим RESTRICTED напрямую (не гоняясь за тем, натурально ли
    реальный текст доходит сюда мимо keyword-гейта) и подменяем escalation_urgency_for на
    контролируемое "calm", чтобы проверить именно то, что чинили: значение реально считается
    и передаётся, а не отбрасывается на дефолт."""

    import app.services.chat_service as chat_service_module

    async def fake_restricted(request, message, context):
        return "RESTRICTED", "test-request-id"

    monkeypatch.setattr(chat_service_module, "classify_consultation_risk", fake_restricted)
    monkeypatch.setattr(chat_service_module, "escalation_urgency_for", lambda message: "calm")

    payload = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "у меня жирная кожа и расширенные поры, что подойдёт и почём?",
        },
    ).json()

    assert "103" not in payload["answer"]
    assert "скорую" not in payload["answer"]


def test_cosmetic_multi_candidate_booking_followup_reoffers_same_services(test_client, managed_env) -> None:
    """Тот же аудитный диалог, шаг 'а когда можно записаться?' — раньше показывал ПОЛНЫЙ
    каталог услуг ("Внутривенный лазер Шатл Комби" в списке для записи на морщины вокруг
    глаз — прямая цитата из аудита), а не те 3 варианта, что только что предложили."""

    _copy_rosh_import_demo_for_chat_test(test_client, managed_env)
    test_client.app.state.llm_client = _ArticleGuidanceLLMClient("Рекомендую вам Филлеры, это вам подходит.")

    first_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        message="здравствуйте, хочу убрать морщины вокруг глаз",
    )

    followup_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        session_id=first_payload["session_id"],
        message="а когда можно записаться?",
    )

    assert followup_payload["action"] == "clarify"
    followup_labels = _quick_action_labels(followup_payload)
    assert {"Ботулинотерапия", "Биоревитализация", "Филлеры"} <= set(followup_labels)
    assert "Внутривенный лазер Шатл Комби" not in followup_labels


def test_engagement_offer_appears_on_5_8_13_substantive_messages(test_client) -> None:
    messages = [
        "покажи услуги",
        "сколько стоит Консультация косметолога",
        "расскажи про Консультация косметолога",
        "сколько стоит Консультация дерматолога",
        "расскажи про Консультация дерматолога",
        "что входит в Консультация косметолога",
        "как долго Консультация косметолога",
        "цена на Консультация дерматолога",
        "покажи услуги",
        "расскажи про Консультация косметолога",
        "сколько стоит Консультация косметолога",
        "что входит в Консультация дерматолога",
        "как долго Консультация дерматолога",
        "цена на Консультация косметолога",
    ]
    session_id = None
    payloads = []
    for message in messages:
        payload = _post_chat(test_client, message=message, session_id=session_id)
        session_id = payload["session_id"]
        payloads.append(payload)

    assert "Консультация дерматолога" in payloads[4]["answer"]
    assert "Вижу, диалог уже длинный" in payloads[4]["answer"]
    assert _quick_action_labels(payloads[4]) == ["Передать администратору", "Продолжить тут"]
    assert "могу передать администратору краткое резюме" in payloads[7]["answer"]
    assert _quick_action_labels(payloads[7]) == ["Передать администратору", "Продолжить тут"]
    assert "Ещё раз на всякий случай предложу" in payloads[12]["answer"]
    assert _quick_action_labels(payloads[12]) == ["Передать администратору", "Продолжить тут"]
    assert "подключился администратор" not in payloads[13]["answer"]
    assert "краткое резюме" not in payloads[13]["answer"]
    assert "Ещё раз" not in payloads[13]["answer"]


def test_engagement_offer_ignores_small_talk(test_client) -> None:
    session_id = None
    payload = {}
    for _ in range(5):
        payload = _post_chat(test_client, message="привет", session_id=session_id)
        session_id = payload["session_id"]

    assert "диалог уже длинный" not in payload["answer"]
    assert _quick_action_labels(payload) != ["Передать администратору", "Продолжить тут"]


def test_engagement_offer_does_not_interrupt_booking_contact_prompt(test_client) -> None:
    session_id = None
    for message in [
        "покажи услуги",
        "сколько стоит Консультация косметолога",
        "расскажи про Консультация косметолога",
        "сколько стоит Консультация дерматолога",
    ]:
        payload = _post_chat(test_client, message=message, session_id=session_id)
        session_id = payload["session_id"]

    payload = _post_chat(test_client, message="хочу записаться", session_id=session_id)

    assert "напишите имя, телефон" in payload["answer"].lower()
    assert "диалог уже длинный" not in payload["answer"]
    assert _quick_action_labels(payload) != ["Передать администратору", "Продолжить тут"]


def test_engagement_offer_does_not_show_after_lead_created(test_client) -> None:
    session_id = None
    for message in [
        "покажи услуги",
        "сколько стоит Консультация косметолога",
        "хочу оставить телефон",
        "Иван +7 999 123-45-67",
        "расскажи про Консультация косметолога",
        "покажи услуги",
    ]:
        payload = _post_chat(test_client, message=message, session_id=session_id)
        session_id = payload["session_id"]

    assert payload["lead_created"] is False
    assert "диалог уже длинный" not in payload["answer"]
    assert _quick_action_labels(payload) != ["Передать администратору", "Продолжить тут"]


def test_engagement_continue_here_does_not_disable_later_offer(test_client) -> None:
    session_id = None
    for message in [
        "покажи услуги",
        "сколько стоит Консультация косметолога",
        "расскажи про Консультация косметолога",
        "сколько стоит Консультация дерматолога",
        "расскажи про Консультация дерматолога",
    ]:
        payload = _post_chat(test_client, message=message, session_id=session_id)
        session_id = payload["session_id"]

    assert "диалог уже длинный" in payload["answer"]

    payload = _post_chat(test_client, message="Продолжить тут", session_id=session_id)
    stored_session = test_client.app.state.session_store._sessions[session_id]

    assert payload["action"] == "clarify"
    assert "продолжим здесь" in payload["answer"].lower()
    assert "услуги центра" not in payload["answer"].lower()
    assert _quick_action_labels(payload) == []
    assert stored_session.substantive_message_count == 5
    assert stored_session.engagement_offer_count == 1

    for message in [
        "что входит в Консультация косметолога",
        "как долго Консультация косметолога",
    ]:
        payload = _post_chat(test_client, message=message, session_id=session_id)

    assert "могу передать администратору краткое резюме" not in payload["answer"]

    payload = _post_chat(test_client, message="цена на Консультация дерматолога", session_id=session_id)

    assert "могу передать администратору краткое резюме" in payload["answer"]
    assert _quick_action_labels(payload) == ["Передать администратору", "Продолжить тут"]


def test_regulated_soft_offer_keeps_referral_service_button(test_client, managed_env) -> None:
    _copy_rosh_import_demo_for_chat_test(test_client, managed_env)

    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_import_demo",
            "session_id": None,
            "message": "можно у вас удалить родинку",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "clarify"
    assert payload["status"] == "AI_ACTIVE"
    assert payload["quick_actions"][0] == {
        "label": "Консультации",
        "type": "message",
        "value": "Консультации",
    }
    # 2026-08-18: клиент подтвердил, что тема новообразований (родинки/папилломы/бородавки)
    # не настолько деликатная, чтобы прятать саму услугу удаления — хэндоф оператору
    # остаётся (это про потенциальную онкологическую настороженность), но услугу теперь
    # тоже показываем второй кнопкой, см. _growth_removal_service_for_referral.
    assert [action["label"] for action in payload["quick_actions"][1:]] == [
        "Удаление новообразований",
        "Оставить телефон",
        "Подключить менеджера",
    ]


def test_external_prescription_soft_offer_keeps_referral_service_button(test_client, managed_env) -> None:
    _copy_rosh_import_demo_for_chat_test(test_client, managed_env)

    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_import_demo",
            "session_id": None,
            "message": "мне назначил другой врач капельницу, можно проконсультироваться?",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "clarify"
    assert payload["status"] == "AI_ACTIVE"
    assert payload["quick_actions"][0]["label"] == "Консультации"


def test_regulated_soft_offer_without_referral_keeps_default_buttons(test_client) -> None:
    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "у меня воспаление что делать",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "clarify"
    assert [action["label"] for action in payload["quick_actions"]] == [
        "Оставить телефон",
        "Подключить менеджера",
    ]


def test_regulated_soft_contact_creates_flagged_lead(test_client, managed_env) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "у меня воспаление что делать",
        },
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +7 999 123-45-67",
        },
    )
    second_payload = second_response.json()
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])

    assert second_response.status_code == 200
    assert second_payload["action"] == "ask_contact"
    assert second_payload["lead_created"] is True
    assert second_payload["status"] == "AI_ACTIVE"
    assert lead["reason"] == "medical_risk"
    assert lead["needs_operator"] is True
    assert lead["lead_trigger"] == "regulated_advice"


def test_regulated_lead_mode_normal_creates_default_lead(test_client, managed_env) -> None:
    config_path = managed_env["clients_dir"] / "rosh_demo" / "config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").rstrip()
        + "\n  regulated_lead_mode: normal\n",
        encoding="utf-8",
    )
    test_client.app.state.knowledge_base_resolver._cache.clear()

    first_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "у меня воспаление что делать",
        },
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +7 999 123-45-67",
        },
    )
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])

    assert second_response.status_code == 200
    assert lead["reason"] == "commercial_interest"
    assert lead["needs_operator"] is False
    assert lead["lead_trigger"] == "ask_contact"


def test_regulated_instant_override_enqueues_operator_requested_event(
    test_client,
    managed_env,
    monkeypatch,
) -> None:
    config_path = managed_env["clients_dir"] / "rosh_demo" / "config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").rstrip()
        + "\n  regulated_escalation: instant\n",
        encoding="utf-8",
    )
    test_client.app.state.knowledge_base_resolver._cache.clear()
    events = []

    async def fake_enqueue_event(**kwargs):
        events.append(kwargs)
        return []

    monkeypatch.setattr(test_client.app.state.delivery_service, "enqueue_event", fake_enqueue_event)

    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "у меня воспаление что делать",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "transfer_operator"
    assert payload["status"] == "WAITING_OPERATOR"
    assert events[-1]["event_type"] == "operator_requested"
    assert events[-1]["payload"]["last_message"] == "у меня воспаление что делать"
    operator_url = events[-1]["payload"]["operator_url"]
    assert "/operator?token=" in operator_url
    assert "demo-operator-token" in operator_url


def test_operator_second_request_transfers_after_soft_offer(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "оператор"},
    )
    first_payload = first_response.json()
    assert first_response.status_code == 200
    assert first_payload["action"] == "clarify"

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Да, оператора",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "transfer_operator"
    assert second_payload["status"] == "WAITING_OPERATOR"


def test_waiting_operator_generic_followup_gets_acknowledgment_not_silence(test_client) -> None:
    """Живой репро (аудит §2026-08-22): раньше ЛЮБОЕ сообщение в WAITING_OPERATOR, кроме
    контактных данных, получало answer="" — буквальную тишину."""

    session_id = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "оператор"},
    ).json()["session_id"]
    test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "Да, оператора"},
    )

    followup = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "а где вы находитесь?"},
    ).json()

    assert followup["answer"] != ""
    assert "администратор" in followup["answer"].lower()


def test_waiting_operator_complaint_followup_gets_distinct_acknowledgment(test_client) -> None:
    """Та же тишина, но конкретно на жалобу/угрозу отзывом — самый плохой случай молчать
    именно тут (уже разозлённый клиент). Текст должен отличаться от обычного follow-up,
    не просто "не пустая строка" — подтверждает, что differentiate реально сработал."""

    session_id = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "оператор"},
    ).json()["session_id"]
    test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "Да, оператора"},
    )

    complaint_followup = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": session_id,
            "message": "верните деньги, обращусь в Роспотребнадзор",
        },
    ).json()
    generic_followup = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "а вы сегодня работаете?"},
    ).json()

    assert complaint_followup["answer"] != ""
    assert complaint_followup["answer"] != generic_followup["answer"]


def _age_last_message(test_client, session_id: str, minutes: float) -> None:
    from datetime import datetime, timedelta

    stored_session = test_client.app.state.session_store._sessions[session_id]
    stored_session.messages[-1].created_at = datetime.utcnow() - timedelta(minutes=minutes)


def test_operator_wait_timeout_offers_return_to_bot(test_client) -> None:
    session_id = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "оператор"},
    ).json()["session_id"]
    test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "Да, оператора"},
    )
    _age_last_message(test_client, session_id, minutes=6)

    followup = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "а вы ещё тут?"},
    ).json()

    assert "оператор" in followup["answer"].lower()
    values = {action["value"] for action in followup["quick_actions"]}
    assert "Да, продолжить с ботом" in values
    stored_session = test_client.app.state.session_store._sessions[session_id]
    assert stored_session.operator_return_offered is True


def test_operator_wait_timeout_offer_fires_only_once_per_session(test_client) -> None:
    session_id = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "оператор"},
    ).json()["session_id"]
    test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "Да, оператора"},
    )
    _age_last_message(test_client, session_id, minutes=6)
    test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "а вы ещё тут?"},
    )

    second_followup = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "ну что там?"},
    ).json()

    assert second_followup["quick_actions"] == []


def test_operator_return_confirmation_flips_status_back_to_ai_active(test_client) -> None:
    from app.models import SessionStatus

    session_id = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "оператор"},
    ).json()["session_id"]
    test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "Да, оператора"},
    )
    _age_last_message(test_client, session_id, minutes=6)
    test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "а вы ещё тут?"},
    )

    confirm = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": session_id,
            "message": "Да, продолжить с ботом",
        },
    ).json()

    assert confirm["status"] == SessionStatus.AI_ACTIVE.value
    stored_session = test_client.app.state.session_store._sessions[session_id]
    assert stored_session.status == SessionStatus.AI_ACTIVE

    followup = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": session_id, "message": "сколько стоит чистка лица?"},
    ).json()
    assert followup["status"] == SessionStatus.AI_ACTIVE.value
    assert followup["answer"] != ""


def test_price_and_hesitation_objections_share_backoff_end_to_end(test_client) -> None:
    """Полный цикл через реальный /api/chat/message, точные формулировки из транскрипта
    живого QA-аудита (§2026-08-22, P3_2 → P3_4 → P3_8) — не только policy-юнит с ручным
    сидом счётчика, а реальный инкремент через session_store на каждом шаге."""

    session_id = None
    for message in ("3000 рублей — это дорого", "мне надо подумать, возможно завтра напишу"):
        payload = test_client.post(
            "/api/chat/message",
            json={"company_id": "rosh_demo", "session_id": session_id, "message": message},
        ).json()
        session_id = payload["session_id"]
        assert payload["action"] == "answer"

    third = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": session_id,
            "message": "спасибо, подумаю ещё, надо решить, всё дорого",
        },
    ).json()

    assert "не буду торопить" in third["answer"].lower()


def test_pending_contact_accepts_messy_phone_and_name(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу оставить телефон"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "89999229333 леха",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "ask_contact"
    assert second_payload["status"] == "AI_ACTIVE"
    assert second_payload["lead_created"] is True


def test_pending_contact_lead_summary_keeps_prior_user_request(test_client, managed_env) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу татуаж"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +79991234567",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["service_id"] is None
    assert lead["lead_trigger"] == "unknown_service"
    assert lead["reason"] == "unknown_service"
    assert lead["unresolved_query"] == "хочу татуаж"
    assert "татуаж" in lead["summary"].lower()
    assert "неподтверждённую услугу" in lead["summary"]


def test_lead_summary_fallback_removes_contact_details_but_keeps_tail() -> None:
    chat_service = object.__new__(ChatService)
    current_message = "Иван +7 999 123-45-67 завтра утром"
    session = Session(
        company_id="rosh_demo",
        messages=[
            Message(role=MessageRole.USER, text="хочу записаться на чистку лица"),
            Message(role=MessageRole.USER, text=current_message),
        ],
    )

    summary = chat_service._lead_summary(
        session,
        current_message,
        is_booking_request=True,
        name="Иван",
        phone="+79991234567",
    )

    assert summary == "Заявка на запись: хочу записаться на чистку лица (завтра утром)"
    assert "Иван" not in summary
    assert "999" not in summary


def test_unknown_service_booking_phone_creates_marked_lead(test_client, managed_env) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу записаться на биоиоиои"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +79991234567",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["service_id"] is None
    assert lead["lead_trigger"] == "unknown_service"
    assert lead["reason"] == "unknown_service"
    assert "биоиоиои" in lead["unresolved_query"]
    assert "биоиоиои" in lead["summary"]


def test_regular_contact_lead_is_not_marked_unknown_service(test_client, managed_env) -> None:
    response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "Иван +79991234567 хочу узнать"},
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["lead_trigger"] == "ask_contact"
    assert lead["reason"] == "commercial_interest"
    assert lead["unresolved_query"] == ""


def test_ask_contact_lead_summary_fallback_does_not_leak_name(
    test_client,
    managed_env,
    monkeypatch,
) -> None:
    # тот же сценарий, что в test_regular_contact_lead_is_not_marked_unknown_service
    # (имя+телефон+намерение одним сообщением, PolicyAction.ASK_CONTACT), но с падающим
    # LLM-саммаризатором — проверяет, что fallback в _lead_summary() тоже не тащит имя
    # в хвост, а не только путь через pending-contact.
    monkeypatch.setattr(test_client.app.state, "llm_client", _FailingSummaryLLMClient())

    response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "Иван +79991234567 хочу узнать"},
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert "Иван" not in lead["summary"]
    assert "999" not in lead["summary"]


def test_offdomain_phone_same_message_creates_unknown_service_lead(test_client, managed_env) -> None:
    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "Леха, 89990000000, нужна уборка после ремонта",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "ask_contact"
    assert payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["name"] == "Леха"
    assert lead["phone"] == "+79990000000"
    assert lead["service_id"] is None
    assert lead["lead_trigger"] == "unknown_service"
    assert lead["reason"] == "unknown_service"
    assert "уборка после ремонта" in lead["unresolved_query"].lower()
    assert "неподтверждённую услугу" in lead["summary"]


def test_real_service_phone_same_message_stays_regular_lead(test_client, managed_env) -> None:
    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "Иван 89990000000 хочу на чистку лица",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["service_id"] == "facial_cleansing"
    assert lead["lead_trigger"] == "ask_contact"
    assert lead["reason"] == "commercial_interest"
    assert lead["unresolved_query"] == ""


def test_direct_phone_after_price_question_keeps_prior_reason_and_service(test_client, managed_env) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "сколько стоит чистка лица"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +79991234567",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["reason"] == "price_question"
    assert lead["service_id"] == "facial_cleansing"


def test_contact_request_with_phone_in_same_message_creates_lead(test_client, managed_env) -> None:
    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "хочу оставить телефон\n89999229333 леха",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "ask_contact"
    assert payload["status"] == "AI_ACTIVE"
    assert payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["name"] == "Леха"
    assert lead["phone"] == "+79999229333"


def test_unknown_booking_service_with_phone_creates_marked_lead(test_client, managed_env) -> None:
    response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "хочу записаться на шиномонтаж\n89999229333 леха",
        },
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["action"] == "ask_contact"
    assert payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["service_id"] is None
    assert lead["lead_trigger"] == "unknown_service"
    assert lead["reason"] == "unknown_service"
    assert "шиномонтаж" in lead["unresolved_query"].lower()


def test_unknown_service_booking_prompt_keeps_unresolved_query_in_lead(test_client, managed_env) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "татуаж делаете?"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "ну хочу записаться",
        },
    )
    second_payload = second_response.json()

    third_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван 89990000000",
        },
    )
    third_payload = third_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "clarify"
    assert third_response.status_code == 200
    assert third_payload["action"] == "ask_contact"
    assert third_payload["lead_created"] is True
    lead = json.loads((managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert lead["service_id"] is None
    assert lead["lead_trigger"] == "unknown_service"
    assert lead["reason"] == "unknown_service"
    assert lead["unresolved_query"] == "татуаж делаете?"
    assert "татуаж" in lead["summary"].lower()


def test_second_booking_lead_summary_does_not_bleed_previous_booking_context(
    test_client,
    managed_env,
    monkeypatch,
) -> None:
    _copy_rosh_import_demo_for_chat_test(test_client, managed_env)
    monkeypatch.setattr(test_client.app.state, "llm_client", _EchoSummaryLLMClient())

    first_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        message="хочу записаться к Молотиловой",
    )
    session_id = first_payload["session_id"]
    first_service_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        session_id=session_id,
        message="Биоревитализация",
    )
    first_contact_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        session_id=session_id,
        message="Иван 89990000001 к Молотиловой",
    )

    second_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        session_id=session_id,
        message="запишите к Сарычеву на вторник",
    )
    second_service_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        session_id=session_id,
        message="Консультации",
    )
    second_contact_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        session_id=session_id,
        message="Иван 2 89990000002 к Сарычеву во вторник",
    )

    assert first_payload["action"] == "clarify"
    assert "На какую услугу хотите оставить заявку" in first_payload["answer"]
    assert first_service_payload["action"] == "clarify"
    assert "напишите имя, телефон" in first_service_payload["answer"].lower()
    assert first_contact_payload["lead_created"] is True
    assert second_payload["action"] == "clarify"
    assert "На какую услугу хотите оставить заявку" in second_payload["answer"]
    assert second_service_payload["action"] == "clarify"
    assert "напишите имя, телефон" in second_service_payload["answer"].lower()
    assert second_contact_payload["lead_created"] is True
    leads = [
        json.loads(line)
        for line in (managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    first_lead = leads[-2]
    second_lead = leads[-1]

    assert "biorevitalizaciya" in first_lead["service_id"]
    assert "konsultacii" in second_lead["service_id"]
    assert "Молотиловой" in first_lead["summary"]
    assert "Биоревитализация" in first_lead["summary"]
    assert "Сарычеву" in second_lead["summary"]
    assert "Консультации" in second_lead["summary"]
    assert "Молотилов" not in second_lead["summary"]
    assert "Биоревитализация" not in second_lead["summary"]
    recent_text = " | ".join(message["text"] for message in second_lead["recent_messages"])
    assert "Сарычеву" in recent_text
    assert "Молотилов" not in recent_text
    assert "Биоревитализация" not in recent_text


def test_skipped_booking_service_selection_does_not_reuse_previous_service_id(
    test_client,
    managed_env,
    monkeypatch,
) -> None:
    _copy_rosh_import_demo_for_chat_test(test_client, managed_env)
    monkeypatch.setattr(test_client.app.state, "llm_client", _EchoSummaryLLMClient())

    first_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        message="хочу записаться к Молотиловой",
    )
    session_id = first_payload["session_id"]
    _post_chat(
        test_client,
        company_id="rosh_import_demo",
        session_id=session_id,
        message="Биоревитализация",
    )
    first_contact_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        session_id=session_id,
        message="Иван 89990000001 к Молотиловой",
    )

    second_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        session_id=session_id,
        message="запишите к Сарычеву на вторник",
    )
    second_contact_payload = _post_chat(
        test_client,
        company_id="rosh_import_demo",
        session_id=session_id,
        message="Иван 2 89990000002 к Сарычеву во вторник",
    )

    assert first_contact_payload["lead_created"] is True
    assert second_payload["action"] == "clarify"
    assert "На какую услугу хотите оставить заявку" in second_payload["answer"]
    assert second_contact_payload["lead_created"] is True
    leads = [
        json.loads(line)
        for line in (managed_env["temp_dir"] / "leads.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    first_lead = leads[-2]
    second_lead = leads[-1]

    assert "biorevitalizaciya" in first_lead["service_id"]
    assert second_lead["service_id"] is None
    assert "Сарычеву" in second_lead["summary"]
    assert "Биоревитализация" not in second_lead["summary"]
    recent_text = " | ".join(message["text"] for message in second_lead["recent_messages"])
    assert "Сарычеву" in recent_text
    assert "Биоревитализация" not in recent_text


def test_partial_phone_does_not_go_to_llm(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу оставить телефон"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "8999922933 леха",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "clarify"
    assert second_payload["lead_created"] is False
    assert "номер неполный" in second_payload["answer"].lower()


def test_explicit_service_beats_previous_service_context(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "Консультация косметолога",
        },
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "сколько стоит Консультация дерматолога",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "answer"
    assert "Консультация дерматолога" in second_payload["answer"]


def test_context_frame_keeps_service_for_short_price_followup(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": None,
            "message": "Консультация косметолога",
        },
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "а всё-таки почём?",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] == "answer"
    assert "Консультация косметолога" in second_payload["answer"]


def test_context_frame_keeps_doctor_topic_for_followup(test_client) -> None:
    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "кто у вас гинеколог?"},
    )
    first_payload = first_response.json()

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "а дерматолог кто?",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert second_payload["action"] in {"answer", "clarify"}
    assert "врач" in second_payload["answer"].lower() or "специалист" in second_payload["answer"].lower()


def test_new_question_breaks_out_of_pending_booking_contact(test_client) -> None:
    """regression: до фикса любое сообщение после 'хочу записаться' (без телефона)
    навсегда перехватывалось запросом телефона, а extract_name вытаскивал случайное
    слово вопроса как "имя" (например "Какие, напишите телефон..." из "какие врачи
    у вас есть?"). Новый вопрос должен реально отвечаться, не зацикливать."""

    first_response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "хочу записаться на чистку лица"},
    )
    session_id = first_response.json()["session_id"]

    second_response = test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": session_id,
            "message": "какие врачи у вас есть?",
        },
    )
    second_payload = second_response.json()

    assert second_response.status_code == 200
    assert "Какие" not in second_payload["answer"]
    assert "напишите, пожалуйста, телефон — передам заявку" not in second_payload["answer"]


def test_every_answer_is_logged_to_analytics_not_just_exceptions(test_client, managed_env) -> None:
    """Раньше analytics.jsonl писал только исключения (unknown_service/operator/regulated) —
    разбор "бот ответил ерунду" был невозможен для обычного, успешного ответа. Каждое
    сообщение должно логироваться с текстом ответа, не только проблемные."""

    response = _post_chat(test_client, message="привет")
    assert response["action"] != ""  # обычный small_talk, не исключение

    analytics_path = managed_env["temp_dir"] / "analytics.jsonl"
    events = [json.loads(line) for line in analytics_path.read_text(encoding="utf-8").splitlines()]
    answered = [event for event in events if event["event_type"] == "message_answered"]

    assert len(answered) == 1
    assert answered[0]["message"] == "привет"
    assert answered[0]["metadata"]["answer"] == response["answer"]
    assert answered[0]["metadata"]["action"] == response["action"]


def test_analytics_logs_answer_even_for_unknown_service_exception_path(test_client, managed_env) -> None:
    """message_answered должен писаться ВСЕГДА, включая уже-логируемые исключения — не
    заменяет track_policy_result, а дополняет его текстом реального ответа."""

    response = _post_chat(test_client, message="делаете ли вы татуаж бровей")

    analytics_path = managed_env["temp_dir"] / "analytics.jsonl"
    events = [json.loads(line) for line in analytics_path.read_text(encoding="utf-8").splitlines()]
    event_types = {event["event_type"] for event in events}

    assert "message_answered" in event_types
    answered = next(event for event in events if event["event_type"] == "message_answered")
    assert answered["metadata"]["answer"] == response["answer"]


def test_message_over_max_length_is_rejected(test_client) -> None:
    """Раньше сообщение любой длины принималось целиком — попадало в историю сессии, в промпт
    LLM и в логи. 4000 символов — с запасом под реальные вопросы, но не безлимит."""

    response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "а" * 4001},
    )

    assert response.status_code == 422


def test_message_at_max_length_is_accepted(test_client) -> None:
    response = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "привет " * 500},
    )

    assert response.status_code == 200


class _FakeTelegramBridge:
    enabled = True

    def __init__(self) -> None:
        self.queue_cards: list[dict] = []
        self.client_cards: list[str] = []

    async def post_operator_queue_card(self, *, session_id, reason, last_message, client_label):
        self.queue_cards.append(
            {
                "session_id": session_id,
                "reason": reason,
                "last_message": last_message,
                "client_label": client_label,
            }
        )

    async def post_client_lead_card(self, card_text, *, session_id: str = ""):
        self.client_cards.append(card_text)


def test_plain_lead_without_operator_need_posts_to_clients_topic(test_client) -> None:
    """Лид без needs_operator (контакт зафиксирован, бот уже ответил) — карточка в тему
    "Клиенты", НЕ очередь General с клеймом: никто не ждёт живого человека прямо сейчас."""

    fake_bridge = _FakeTelegramBridge()
    test_client.app.state.telegram_bridge_service = fake_bridge

    first_payload = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "Хочу оставить телефон"},
    ).json()
    test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +79991234567",
        },
    )

    assert len(fake_bridge.client_cards) == 1
    assert fake_bridge.queue_cards == []
    assert "Иван" in fake_bridge.client_cards[0]


def test_plain_lead_card_includes_short_id_tag_from_session_id(test_client) -> None:
    """Живой баг (ручное тестирование пользователем, 2026-08-26): короткий id для лидов
    добавили в delivery.py (_telegram_text), но реальные карточки простых лидов уходят через
    ДРУГОЙ, отдельный путь — _notify_telegram_for_lead -> telegram_bridge.post_client_lead_card
    в chat_service.py, у него свой собственный card_text, delivery.py тут вообще не участвует.
    Тег строился не в том месте — в живом Telegram он не появлялся ни разу."""

    fake_bridge = _FakeTelegramBridge()
    test_client.app.state.telegram_bridge_service = fake_bridge

    first_payload = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "Хочу оставить телефон"},
    ).json()
    session_id = first_payload["session_id"]
    test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": session_id,
            "message": "Иван +79991234567",
        },
    )

    expected_short_id = session_id.replace("-", "")[-6:]
    assert len(fake_bridge.client_cards) == 1
    assert f"· #{expected_short_id}" in fake_bridge.client_cards[0]


def test_regulated_lead_with_operator_need_posts_to_general_queue(test_client) -> None:
    """Лид с needs_operator=True (регулируемый флоу) — карточка в General с клеймом, как
    прямой operator_requested: тут реально нужен живой человек, не просто лог контакта."""

    fake_bridge = _FakeTelegramBridge()
    test_client.app.state.telegram_bridge_service = fake_bridge

    first_payload = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "у меня воспаление что делать"},
    ).json()
    test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Иван +7 999 123-45-67",
        },
    )

    assert fake_bridge.client_cards == []
    assert len(fake_bridge.queue_cards) == 1
    assert fake_bridge.queue_cards[0]["client_label"] == "Иван"


def test_direct_operator_request_posts_to_general_queue(test_client) -> None:
    fake_bridge = _FakeTelegramBridge()
    test_client.app.state.telegram_bridge_service = fake_bridge

    first_payload = test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": None, "message": "оператор"},
    ).json()
    test_client.post(
        "/api/chat/message",
        json={
            "company_id": "rosh_demo",
            "session_id": first_payload["session_id"],
            "message": "Да, оператора",
        },
    )

    assert len(fake_bridge.queue_cards) == 1
    assert fake_bridge.queue_cards[0]["reason"] == "⚡️ Запросил оператора"
    assert fake_bridge.client_cards == []
