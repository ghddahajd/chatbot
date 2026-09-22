"""живые баги 2026-09-23 (подтверждены на проде через /api/debug/trace):

1. «какой у вас телефон», «как с вами связаться», «до скольки вы работаете» получали
   «что вас интересует?» — бот не давал ни телефон, ни часы;
2. после «…Или сразу соединю с менеджером» согласие текстом не соединяло: «да» уходило в
   «что уточнить по этой услуге», «соедините» — в ветку «отказался от оператора».
"""

from __future__ import annotations

import pytest

from app.models import PendingAction, PolicyAction, PolicyReason, Session
from app.policy import analyze_message, classify_and_extract
from app.routes.chat_utils import contextual_affirmative_response


def _classification(message: str, knowledge_base) -> dict[str, object]:
    return classify_and_extract(
        message,
        [service.model_dump() for service in knowledge_base.services],
        knowledge_base.company.city,
        knowledge_base.domain_profile,
    )


def _analyze(message: str, knowledge_base, session: Session | None = None, classification=None):
    session = session or Session(company_id="rosh_demo")
    return analyze_message(message, session, knowledge_base, classification or _classification(message, knowledge_base))


@pytest.fixture()
def always_open(knowledge_base):
    # пустое расписание = «всегда открыто» (контракт is_currently_open) — тесты не зависят от часов машины
    knowledge_base.company.working_hours_schedule = {}
    return knowledge_base


# ---------------------------------------------------------------- телефон и часы


@pytest.mark.parametrize(
    "message",
    [
        "какой у вас телефон",
        "какой у вас телефон?",
        "дайте номер телефона",
        "как с вами связаться",
        "ваш телефон",
        "номер телефона клиники",
        "контакты",
        "телефон",
        "подскажите номер телефона",
        "куда позвонить",
    ],
)
def test_phone_question_gets_the_clinic_phone(message: str, knowledge_base) -> None:
    result = _analyze(message, knowledge_base)

    assert result.action == PolicyAction.ANSWER
    assert result.safe_context.get("clinic_info_topic") == "contacts"
    text = result.safe_context["message_to_user"]
    assert knowledge_base.company.phone in text
    assert knowledge_base.company.working_hours in text
    assert result.quick_actions == ["Позвать менеджера", "Написать в Telegram"]


def test_placeholder_address_is_not_quoted(knowledge_base) -> None:
    """в фикстуре адрес «уточняется оператором» — его не цитируем, только телефон и часы."""

    text = _analyze("какой у вас телефон", knowledge_base).safe_context["message_to_user"]

    assert "уточняется" not in text
    assert "Адрес" not in text


def test_real_address_is_included(knowledge_base) -> None:
    knowledge_base.company.address = "Москва, Ростовская набережная, 5"

    text = _analyze("как с вами связаться", knowledge_base).safe_context["message_to_user"]

    assert "Ростовская набережная, 5" in text


def test_client_phrasebook_can_override_contacts_text(knowledge_base) -> None:
    knowledge_base.phrasebook["clinic_contacts_deferred"] = "Звоните: {phone}"

    assert _analyze("ваш телефон", knowledge_base).safe_context["message_to_user"] == f"Звоните: {knowledge_base.company.phone}"


def test_no_phone_in_data_falls_back_to_previous_behaviour(knowledge_base) -> None:
    knowledge_base.company.phone = ""

    result = _analyze("какой у вас телефон", knowledge_base)

    assert result.safe_context.get("clinic_info_topic") != "contacts"


@pytest.mark.parametrize(
    "message",
    [
        "мой телефон 926 123 45 67",
        "запишите мой номер 89261234567",
        "оставлю телефон",
        "оставлю номер телефона",
        "перезвоните мне",
        "перезвоните на мой номер",
        # найдено сетью безопасности 2026-09-23: исключение было по «оставл» и не ловило «оставить»
        "оставить номер телефона",
        "номер телефона оставить",
        "могу оставить свой номер",
    ],
)
def test_client_giving_own_contact_is_not_a_phone_question(message: str, knowledge_base) -> None:
    result = _analyze(message, knowledge_base)

    assert result.safe_context.get("clinic_info_topic") != "contacts"


@pytest.mark.parametrize(
    "message",
    ["до скольки вы работаете", "когда вы работаете", "вы работаете в воскресенье?", "время работы", "работаете сегодня?"],
)
def test_hours_questions_get_location_and_hours(message: str, knowledge_base) -> None:
    result = _analyze(message, knowledge_base)

    assert result.safe_context.get("clinic_info_topic") == "location"
    assert knowledge_base.company.working_hours in result.safe_context["message_to_user"]


@pytest.mark.parametrize("message", ["где вы?", "какой у вас адрес", "часы работы", "как добраться"])
def test_existing_location_questions_still_work(message: str, knowledge_base) -> None:
    assert _analyze(message, knowledge_base).safe_context.get("clinic_info_topic") == "location"


def test_own_contact_while_bot_waits_for_booking_contact_is_not_a_phone_question(knowledge_base) -> None:
    """сеть безопасности 2026-09-23: в ожидании контакта для записи «оставить номер телефона»
    получал телефон клиники вместо «оставьте имя и телефон»."""

    session = Session(company_id="rosh_demo")
    session.pending_action = PendingAction.BOOKING_CONTACT.value

    result = _analyze("оставить номер телефона", knowledge_base, session)

    assert result.safe_context.get("clinic_info_topic") != "contacts"


def test_doctors_and_hours_in_one_message_keeps_the_doctors_answer(knowledge_base) -> None:
    """сеть безопасности 2026-09-23: «какие врачи есть и работаете ли по выходным?» раньше получал
    список врачей — новые варианты часов не должны его перехватывать."""

    result = _analyze("а какие врачи есть и работаете ли по выходным?", knowledge_base)

    assert result.safe_context.get("clinic_info_topic") != "location"


def test_doctor_schedule_is_not_hijacked_by_clinic_hours(knowledge_base) -> None:
    """«когда работает врач» — расписание врача, а не часы клиники («когда работаете»)."""

    result = _analyze("когда работает косметолог", knowledge_base)

    assert result.safe_context.get("clinic_info_topic") != "location"


# ---------------------------------------------------------------- согласие на менеджера


def _offered_session() -> Session:
    session = Session(company_id="rosh_demo")
    session.pending_action = PendingAction.OFFERED_OPERATOR.value
    return session


@pytest.mark.parametrize("message", ["да", "ок", "хорошо", "Да!"])
def test_bare_consent_after_offer_connects_operator(message: str, always_open) -> None:
    # голое «да» классифицируется как clarify (chat_utils._clarify_without_model) — так и передаём
    classification = {"intent": "clarify", "service_id": None, "confidence": 0.82}

    result = _analyze(message, always_open, _offered_session(), classification)

    assert result.action == PolicyAction.TRANSFER_OPERATOR
    assert result.reason == PolicyReason.OPERATOR_REQUESTED


def _chat(test_client, message: str, session_id: str | None) -> dict:
    response = test_client.post("/api/chat/message", json={"company_id": "rosh_demo", "session_id": session_id, "message": message})
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize(
    "message",
    ["да", "ок", "давайте", "хорошо", "конечно", "соедините", "да, соедините", "подключите", "да, соедините с менеджером", "Да!"],
)
def test_consent_after_offer_connects_operator_end_to_end(message: str, test_client) -> None:
    """настоящий путь: классификация (с контекстным перехватом «да»), политика, chat_service."""

    test_client.app.state.knowledge_base_resolver.get("rosh_demo", fallback=False).company.working_hours_schedule = {}
    first = _chat(test_client, "позовите оператора", None)
    second = _chat(test_client, message, first["session_id"])

    assert first["action"] == "clarify"
    assert second["action"] == "transfer_operator"


def test_consent_after_offer_at_night_asks_for_contact(knowledge_base) -> None:
    from app.models import DaySchedule

    closed_all_day = DaySchedule(open="00:00", close="00:01")
    knowledge_base.company.working_hours_schedule = {day: closed_all_day for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}

    result = _analyze("да", knowledge_base, _offered_session(), {"intent": "clarify", "service_id": None, "confidence": 0.82})

    assert result.action == PolicyAction.ASK_CONTACT
    assert result.reason == PolicyReason.OPERATOR_REQUESTED


@pytest.mark.parametrize("message", ["сначала спрошу тут", "нет", "да, но сначала сколько стоит пилинг", "давайте позже"])
def test_decline_or_real_question_is_not_consent(message: str, always_open) -> None:
    result = _analyze(message, always_open, _offered_session())

    assert result.action != PolicyAction.TRANSFER_OPERATOR


def test_bare_yes_without_offer_is_unchanged(always_open) -> None:
    result = _analyze("да", always_open, Session(company_id="rosh_demo"), {"intent": "clarify", "service_id": None, "confidence": 0.82})

    assert result.action != PolicyAction.TRANSFER_OPERATOR


def test_affirmative_interceptor_steps_aside_after_operator_offer() -> None:
    offered = _offered_session()
    offered.messages = []
    from app.models import Message, MessageRole

    for session in (offered,):
        session.messages.append(Message(role=MessageRole.USER, text="позовите оператора"))
        session.messages.append(Message(role=MessageRole.USER, text="да"))
    assert contextual_affirmative_response("да", offered) is None

    plain = Session(company_id="rosh_demo")
    plain.messages.append(Message(role=MessageRole.USER, text="чистка лица"))
    plain.messages.append(Message(role=MessageRole.USER, text="да"))
    assert contextual_affirmative_response("да", plain) is not None  # старое поведение вне предложения менеджера
