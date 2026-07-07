"""вспомогательные функции для роутов чата."""

from __future__ import annotations

import logging
import re
import time
from uuid import uuid4

from fastapi import Request

from ..knowledge import KnowledgeBase
from ..llm import MockLLMClient
from ..knowledge import normalize_text
from ..models import Message, MessageRole, PolicyAction, PolicyReason, PolicyResult, QuickAction, Session
from ..policy import classify_and_extract
from ..policy.adapter import merge_policy_classifications, structured_to_policy_classification
from ..policy.constants import (
    AFFIRMATIVE_MESSAGES,
    CLARIFY_SHORT_MESSAGES,
    DURATION_KEYWORDS,
    EXPLANATION_KEYWORDS,
    PRICE_KEYWORDS,
)
from ..policy.extractors import contains_keyword, extract_phone, last_service_from_history, mentions_company_city
from ..policy.restricted import (
    get_restricted_categories,
    has_medical_restricted_category,
    is_restricted_question,
)


fallback_llm_client = MockLLMClient()
logger = logging.getLogger(__name__)
MAX_SESSION_MESSAGES = 30
MAX_MESSAGE_LENGTH = 1000
HAS_LETTER_OR_DIGIT = re.compile(r"[0-9A-Za-zА-Яа-яЁё]")
RATE_LIMIT_ANSWER = (
    "Похоже, разговор затянулся. Если остались вопросы — оставьте телефон, "
    "мы свяжемся, или попробуйте начать новый диалог."
)
CONSULTATION_RISK_RESTRICTED = "RESTRICTED"
CONSULTATION_RISK_SAFE = "SAFE"


def format_quick_actions(
    labels: list[object],
    request: Request,
    knowledge_base: KnowledgeBase | None = None,
) -> list[QuickAction]:
    company = (knowledge_base or request.app.state.knowledge_base).company
    values_by_label = {
        "Позвать менеджера": ("message", "Хочу поговорить с менеджером"),
        "Посмотреть услуги": ("message", "Покажи список услуг"),
        "Оставить телефон": ("message", "Хочу оставить телефон"),
        "Уточнить цену": ("message", "Хочу уточнить цену"),
        "Написать в Telegram": ("link", company.telegram_url or ""),
        "Открыть сайт": ("link", company.website_url or ""),
    }
    normalized_values = {label.casefold(): value for label, value in values_by_label.items()}

    actions: list[QuickAction] = []
    for item in labels:
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            action_type = str(item.get("type") or "message").strip()
            value = str(item.get("value") or label).strip()
            if label and value:
                actions.append(QuickAction(label=label, type=action_type, value=value))
            continue

        label = str(item)
        action_type, value = normalized_values.get(label.casefold(), ("message", label))
        if action_type == "link" and not value:
            continue
        actions.append(QuickAction(label=label, type=action_type, value=value))
    return actions


def service_classifier_payload(
    request: Request,
    knowledge_base: KnowledgeBase | None = None,
) -> list[dict[str, object]]:
    selected_knowledge_base = knowledge_base or request.app.state.knowledge_base
    return [
        {
            "id": service.id,
            "name": service.name,
            "synonyms": service.synonyms,
        }
        for service in selected_knowledge_base.services
    ]


def _last_assistant_text(session: Session) -> str:
    for message in reversed(session.messages[:-1]):
        if message.role == MessageRole.ASSISTANT:
            return normalize_text(message.text)
    return ""


def maybe_contextual_classification(
    message: str,
    session: Session,
) -> dict[str, object] | None:
    normalized_message = normalize_text(message)
    last_assistant_text = _last_assistant_text(session)
    if normalized_message in {"что еще", "что ещё", "а что еще", "а что ещё"} and (
        "доступны услуги" in last_assistant_text
        or "список услуг" in last_assistant_text
        or "услуги:" in last_assistant_text
    ):
        return {"intent": "list_services", "service_id": None, "confidence": 0.88}

    if normalized_message not in AFFIRMATIVE_MESSAGES:
        return None

    if (
        "могу рассказать про услуги" in last_assistant_text
        or "давайте я расскажу про наши услуги" in last_assistant_text
        or "услуги и цены" in last_assistant_text
    ):
        return {"intent": "list_services", "service_id": None, "confidence": 0.9}

    return None


def contextual_affirmative_response(
    message: str,
    session: Session,
) -> PolicyResult | None:
    normalized_message = normalize_text(message)
    if normalized_message not in AFFIRMATIVE_MESSAGES:
        return None

    for history_message in reversed(session.messages[:-1]):
        if history_message.role != MessageRole.USER:
            continue
        if normalize_text(history_message.text) in AFFIRMATIVE_MESSAGES:
            continue
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.OK,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": "Что уточнить по этой услуге: цену, детали или соединить с менеджером?"
            },
            quick_actions=[
                "Уточнить цену",
                {
                    "label": "Подробнее",
                    "type": "message",
                    "value": "Расскажи подробнее про эту услугу",
                },
                "Позвать менеджера",
            ],
        )

    return None


def should_ignore_model_location_mismatch(
    message: str,
    company_city: str,
    local_result: dict[str, object],
    model_result: dict[str, object],
) -> bool:
    """не даёт модели считать mismatch фразы вроде 'я в Москве'."""

    if str(model_result.get("intent") or "") != "location_mismatch":
        return False
    if str(local_result.get("intent") or "") == "location_mismatch":
        return False
    return mentions_company_city(normalize_text(message), company_city)


def should_ignore_model_regulated_advice(
    local_result: dict[str, object],
    model_result: dict[str, object],
    domain_profile: dict[str, object],
) -> bool:
    """не даёт generic/auto домену принять medical-like risk от модели."""

    model_intent = str(model_result.get("intent") or "")
    local_intent = str(local_result.get("intent") or "")
    return (
        model_intent in {"medical_advice", "regulated_advice"}
        and local_intent not in {"medical_advice", "regulated_advice"}
        and not has_medical_restricted_category(domain_profile)
    )


def _clarify_without_model(message: str) -> dict[str, object] | None:
    normalized_message = normalize_text(message)
    if not HAS_LETTER_OR_DIGIT.search(message):
        return {"intent": "clarify", "service_id": None, "confidence": 0.9}
    if (
        normalized_message in AFFIRMATIVE_MESSAGES
        or normalized_message in CLARIFY_SHORT_MESSAGES
        or len(normalized_message) <= 2
    ):
        return {"intent": "clarify", "service_id": None, "confidence": 0.82}
    if "метро" in normalized_message or "рядом" in normalized_message:
        return {"intent": "clarify", "service_id": None, "confidence": 0.78}
    return None


def _contextual_service_classification(
    message: str,
    session: Session | None,
    knowledge_base: KnowledgeBase,
    local_result: dict[str, object],
) -> dict[str, object] | None:
    if session is None:
        return None

    service_id = session.last_service_id or last_service_from_history(session, knowledge_base)
    if not service_id:
        return None

    local_service_id = local_result.get("service_id")
    if local_service_id and local_service_id != service_id:
        return local_result

    normalized_message = normalize_text(message)
    if contains_keyword(normalized_message, PRICE_KEYWORDS) or normalized_message in {"а сколько", "сколько", "почем", "почём"}:
        return {"intent": "price_question", "service_id": service_id, "confidence": 0.9}
    if contains_keyword(normalized_message, DURATION_KEYWORDS) or contains_keyword(
        normalized_message,
        EXPLANATION_KEYWORDS,
    ):
        return {"intent": str(local_result.get("intent") or "service_mention"), "service_id": service_id, "confidence": 0.86}
    return None


def _looks_like_unknown_service_question(message: str) -> bool:
    normalized_message = normalize_text(message)
    return any(
        phrase in normalized_message
        for phrase in {
            "хочу ",
            "есть",
            "делаете",
            "делаите",
            "можно",
            "сколько стоит",
            "цена",
        }
    )


async def resolve_classification(
    message: str,
    request: Request,
    knowledge_base: KnowledgeBase | None = None,
    session: Session | None = None,
) -> dict[str, object]:
    selected_knowledge_base = knowledge_base or request.app.state.knowledge_base
    known_services = service_classifier_payload(request, selected_knowledge_base)
    local_result = classify_and_extract(
        message,
        known_services,
        selected_knowledge_base.company.city,
        selected_knowledge_base.domain_profile,
    )
    local_intent = str(local_result.get("intent") or "")
    local_confidence = float(local_result.get("confidence") or 0.0)
    if local_intent in {"medical_advice", "regulated_advice"}:
        return local_result
    if extract_phone(message):
        return local_result
    clarify_result = _clarify_without_model(message)
    if clarify_result is not None:
        return clarify_result
    contextual_result = _contextual_service_classification(message, session, selected_knowledge_base, local_result)
    if contextual_result is not None:
        return contextual_result

    settings = request.app.state.settings
    skip_local_classifier = settings.llm_skip_classifier_for_local
    if skip_local_classifier is None:
        skip_local_classifier = settings.llm_provider.lower().strip() == "openai_compatible"
    if (
        not settings.llm_use_structured_classifier
        and settings.llm_provider.lower().strip() == "openai_compatible"
        and skip_local_classifier
        and local_confidence > 0
    ):
        return local_result

    structured_result = None
    if settings.llm_use_structured_classifier:
        try:
            structured_result = await request.app.state.llm_client.classify_structured(
                message,
                known_services,
                selected_knowledge_base.domain_profile,
            )
        except Exception as error:
            logger.info("structured_classifier_source=local reason=helper_error error=%s", type(error).__name__)
            return local_result

    if structured_result is None:
        try:
            model_result = await request.app.state.llm_client.classify_and_extract(message, known_services)
        except Exception as error:
            logger.info("classifier_source=local reason=helper_error error=%s", type(error).__name__)
            return local_result
    else:
        model_result = structured_to_policy_classification(structured_result)

    if should_ignore_model_regulated_advice(local_result, model_result, selected_knowledge_base.domain_profile):
        return local_result

    if should_ignore_model_location_mismatch(
        message,
        selected_knowledge_base.company.city,
        local_result,
        model_result,
    ):
        return local_result
    if (
        str(model_result.get("intent") or "") == "off_topic"
        and local_intent == "service_mention"
        and not local_result.get("service_id")
        and _looks_like_unknown_service_question(message)
    ):
        return local_result

    return merge_policy_classifications(local_result, model_result)


async def safe_small_talk(request: Request, company_name: str, message: str) -> str:
    try:
        return await request.app.state.llm_client.small_talk(company_name, message)
    except Exception as error:
        logger.info("small_talk_source=fallback reason=helper_error error=%s", type(error).__name__)
        return await fallback_llm_client.small_talk(company_name, message)


def should_use_consultation_llm(context: dict[str, object]) -> bool:
    if context.get("question_type") == "cosmetic_concern":
        return True
    if context.get("question_type"):
        return False
    if context.get("message_to_user"):
        return False
    service = context.get("service")
    return isinstance(service, dict) and bool(service.get("name"))


async def classify_consultation_risk(
    request: Request,
    message: str,
    context: dict[str, object],
) -> tuple[str, str]:
    """классифицирует риск консультационного ответа относительно domain_profile."""

    request_id = uuid4().hex[:10]
    started_at = time.perf_counter()
    domain_profile = context.get("domain_profile") if isinstance(context.get("domain_profile"), dict) else {}
    categories = get_restricted_categories(domain_profile)
    if not categories:
        return CONSULTATION_RISK_SAFE, request_id

    is_restricted, restricted_category = is_restricted_question(message, domain_profile)
    if is_restricted:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "request_id=%s classification_restricted_ms=%.1f result=%s category=%s source=local",
            request_id,
            elapsed_ms,
            CONSULTATION_RISK_RESTRICTED,
            restricted_category,
        )
        return CONSULTATION_RISK_RESTRICTED, request_id

    if not has_medical_restricted_category(domain_profile):
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "request_id=%s classification_restricted_ms=%.1f result=%s categories=%s source=policy",
            request_id,
            elapsed_ms,
            CONSULTATION_RISK_SAFE,
            ",".join(categories),
        )
        return CONSULTATION_RISK_SAFE, request_id

    local_result = await fallback_llm_client.classify_restricted_risk(message)
    service = context.get("service")
    has_service_context = isinstance(service, dict) and bool(service.get("name"))
    if local_result == "MEDICAL" or has_service_context:
        result = local_result
    else:
        try:
            result = await request.app.state.llm_client.classify_restricted_risk(message)
        except Exception as error:
            logger.info("restricted_classifier_source=local reason=helper_error error=%s", type(error).__name__)
            result = local_result

    normalized_result = str(result or "").strip().upper()
    if normalized_result not in {"MEDICAL", "COSMETIC"}:
        normalized_result = "MEDICAL"
    risk = CONSULTATION_RISK_RESTRICTED if normalized_result == "MEDICAL" else CONSULTATION_RISK_SAFE

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "request_id=%s classification_restricted_ms=%.1f result=%s legacy_result=%s category=medical",
        request_id,
        elapsed_ms,
        risk,
        normalized_result,
    )
    return risk, request_id


async def safe_restricted_handoff(request: Request, message: str) -> str:
    """возвращает безопасный handoff-ответ для restricted тем."""

    del request, message
    return await fallback_llm_client.restricted_handoff("")


async def safe_complete(
    request: Request,
    context: dict[str, object],
    message: str,
    history: list[Message],
) -> str:
    if should_use_consultation_llm(context):
        try:
            return await request.app.state.llm_client.service_consultation(
                context,
                message,
                history,
            )
        except Exception as error:
            logger.info("service_consultation_source=fallback reason=helper_error error=%s", type(error).__name__)
            return await fallback_llm_client.service_consultation(
                context,
                message,
                history,
            )

    try:
        return await request.app.state.llm_client.complete(
            request.app.state.system_prompt,
            context,
            message,
            history,
        )
    except Exception as error:
        logger.info("complete_source=fallback reason=helper_error error=%s", type(error).__name__)
        return await fallback_llm_client.complete(
            request.app.state.system_prompt,
            context,
            message,
            history,
        )
