"""Helpers for chat routes."""

from __future__ import annotations

import re

from fastapi import Request

from ..llm import MockLLMClient
from ..models import Message, QuickAction
from ..policy import classify_and_extract


fallback_llm_client = MockLLMClient()
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


async def safe_complete(
    request: Request,
    context: dict[str, object],
    message: str,
    history: list[Message],
) -> str:
    if not should_use_consultation_llm(context):
        return await fallback_llm_client.complete(
            request.app.state.system_prompt,
            context,
            message,
            history,
        )

    try:
        return await request.app.state.llm_client.service_consultation(
            context,
            message,
        )
    except Exception:
        return await fallback_llm_client.service_consultation(
            context,
            message,
        )
