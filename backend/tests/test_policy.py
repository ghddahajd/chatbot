"""проверки policy guard."""

from app.models import Message, MessageRole, PolicyAction, PolicyReason
from app.policy import analyze_message, classify_and_extract
from app.policy.constants import OPERATOR_SOFT_OFFER_MESSAGE


def _classification(message: str, knowledge_base) -> dict[str, object]:
    return classify_and_extract(
        message,
        [service.model_dump() for service in knowledge_base.services],
        knowledge_base.company.city,
    )


def _analyze(message: str, session, knowledge_base):
    return analyze_message(
        message,
        session,
        knowledge_base,
        _classification(message, knowledge_base),
    )


def test_medical_question_blocked(policy_session, knowledge_base) -> None:
    result = _analyze("что попить от прыщей?", policy_session, knowledge_base)

    assert result.action == PolicyAction.TRANSFER_OPERATOR
    assert result.reason == PolicyReason.MEDICAL_ADVICE


def test_price_with_known_service(policy_session, knowledge_base) -> None:
    result = _analyze("сколько стоит чистка лица?", policy_session, knowledge_base)

    assert result.action == PolicyAction.ANSWER
    assert result.reason == PolicyReason.PRICE_QUESTION
    assert result.service_id == "facial_cleansing"


def test_price_without_service_asks_clarification(policy_session, knowledge_base) -> None:
    result = _analyze("сколько стоит?", policy_session, knowledge_base)

    assert result.action == PolicyAction.CLARIFY
    assert result.reason == PolicyReason.PRICE_QUESTION_NO_SERVICE
    assert result.quick_actions


def test_unknown_service_suggests_similar(policy_session, knowledge_base) -> None:
    result = _analyze("есть пилинг?", policy_session, knowledge_base)

    assert result.action == PolicyAction.CLARIFY
    assert result.reason == PolicyReason.SIMILAR_SERVICES_FOUND
    assert result.safe_context["similar"]


def test_operator_request_soft_redirect(policy_session, knowledge_base) -> None:
    result = _analyze("хочу оператора", policy_session, knowledge_base)

    assert result.action == PolicyAction.CLARIFY
    assert result.reason == PolicyReason.OPERATOR_REQUESTED


def test_operator_request_second_time_hard_transfer(policy_session, knowledge_base) -> None:
    policy_session.messages.append(
        Message(role=MessageRole.ASSISTANT, text=OPERATOR_SOFT_OFFER_MESSAGE)
    )
    result = _analyze("хочу оператора", policy_session, knowledge_base)

    assert result.action == PolicyAction.TRANSFER_OPERATOR
    assert result.reason == PolicyReason.OPERATOR_REQUESTED


def test_geography_not_moscow(policy_session, knowledge_base) -> None:
    result = _analyze("я не из Москвы, можно к вам?", policy_session, knowledge_base)

    assert result.action == PolicyAction.CLARIFY
    assert result.reason == PolicyReason.LOCATION_MISMATCH


def test_small_talk_greeting(policy_session, knowledge_base) -> None:
    result = _analyze("привет", policy_session, knowledge_base)

    assert result.action == PolicyAction.SMALL_TALK
    assert result.reason == PolicyReason.SMALL_TALK


def test_off_topic_bicycle(policy_session, knowledge_base) -> None:
    result = _analyze("слетела цепь на велике", policy_session, knowledge_base)

    assert result.action == PolicyAction.OFF_TOPIC
    assert result.reason == PolicyReason.OFF_TOPIC
