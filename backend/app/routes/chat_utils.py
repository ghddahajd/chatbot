"""Helpers for chat routes."""

from __future__ import annotations

import logging
import re
import time
from uuid import uuid4

from fastapi import Request

from ..llm import MockLLMClient
from ..knowledge import normalize_text
from ..models import Message, MessageRole, PolicyAction, PolicyReason, PolicyResult, QuickAction, Session
from ..policy import classify_and_extract
from ..policy.constants import AFFIRMATIVE_MESSAGES


fallback_llm_client = MockLLMClient()
logger = logging.getLogger(__name__)
MAX_SESSION_MESSAGES = 30
MAX_MESSAGE_LENGTH = 1000
HAS_LETTER_OR_DIGIT = re.compile(r"[0-9A-Za-zА-Яа-яЁё]")
RATE_LIMIT_ANSWER = (
    "Похоже, разговор затянулся. Если остались вопросы — оставьте телефон, "
    "мы свяжемся, или попробуйте начать новый диалог."
)


def format_quick_actions(labels: list[object], request: Request) -> list[QuickAction]:
    company = request.app.state.knowledge_base.company
    values_by_label = {
        "Позвать оператора": ("message", "Хочу поговорить с оператором"),
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


def service_classifier_payload(request: Request) -> list[dict[str, object]]:
    return [
        {
            "id": service.id,
            "name": service.name,
            "synonyms": service.synonyms,
        }
        for service in request.app.state.knowledge_base.services
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
    if normalized_message not in AFFIRMATIVE_MESSAGES:
        return None

    last_assistant_text = _last_assistant_text(session)
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
                "message_to_user": "Что уточнить по этой услуге: цену, детали или соединить со специалистом?"
            },
            quick_actions=[
                "Уточнить цену",
                {
                    "label": "Подробнее",
                    "type": "message",
                    "value": "Расскажи подробнее про эту услугу",
                },
                "Позвать оператора",
            ],
        )

    return None


async def resolve_classification(message: str, request: Request) -> dict[str, object]:
    known_services = service_classifier_payload(request)
    local_result = classify_and_extract(
        message,
        known_services,
        request.app.state.knowledge_base.company.city,
    )
    settings = request.app.state.settings
    skip_local_classifier = settings.llm_skip_classifier_for_local
    if skip_local_classifier is None:
        skip_local_classifier = settings.llm_provider.lower().strip() == "openai_compatible"
    if settings.llm_provider.lower().strip() == "openai_compatible" and skip_local_classifier:
        return local_result

    try:
        model_result = await request.app.state.llm_client.classify_and_extract(message, known_services)
    except Exception:
        return local_result

    model_intent = str(model_result.get("intent") or "")
    local_intent = str(local_result.get("intent") or "")
    model_service_id = model_result.get("service_id")
    local_service_id = local_result.get("service_id")
    model_confidence = float(model_result.get("confidence") or 0.0)
    local_confidence = float(local_result.get("confidence") or 0.0)

    if local_intent in {"unknown_service", "clarify"}:
        return local_result
    if model_intent == "unknown_service":
        return model_result
    if local_service_id and not model_service_id:
        return local_result
    if (
        local_intent
        in {
            "list_services",
            "price_question",
            "medical_advice",
            "operator_request",
            "contact_link",
            "off_topic",
            "cosmetic_concern",
            "location_mismatch",
        }
        and model_intent in {"small_talk", "service_mention"}
        and not model_service_id
    ):
        return local_result
    if model_confidence < 0.5 and local_confidence > model_confidence:
        return local_result
    return model_result


async def safe_small_talk(request: Request, company_name: str, message: str) -> str:
    del request
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


async def classify_consultation_medical_risk(
    request: Request,
    message: str,
    context: dict[str, object],
) -> tuple[str, str]:
    request_id = uuid4().hex[:10]
    started_at = time.perf_counter()
    local_result = await fallback_llm_client.classify_medical_risk(message)
    service = context.get("service")
    has_service_context = isinstance(service, dict) and bool(service.get("name"))
    if local_result == "MEDICAL" or has_service_context:
        result = local_result
    else:
        try:
            result = await request.app.state.llm_client.classify_medical_risk(message)
        except Exception:
            result = local_result

    normalized_result = str(result or "").strip().upper()
    if normalized_result not in {"MEDICAL", "COSMETIC"}:
        normalized_result = "MEDICAL"

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "request_id=%s classification_medical_ms=%.1f result=%s",
        request_id,
        elapsed_ms,
        normalized_result,
    )
    return normalized_result, request_id


async def safe_medical_handoff(request: Request, message: str) -> str:
    del request, message
    return await fallback_llm_client.medical_handoff("")


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
