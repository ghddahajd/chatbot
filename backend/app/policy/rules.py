"""переиспользуемые вспомогательные функции для правил политики."""

from __future__ import annotations

from typing import Any, Optional

from ..knowledge import KnowledgeBase, normalize_text
from ..models import PolicyAction, PolicyReason, PolicyResult
from .constants import COSMETIC_CONCERN_SERVICE_MAP, GENERIC_PRICE_MESSAGES
from .quick_actions import services_summary


def similar_services_result(
    message: str,
    knowledge_base: KnowledgeBase,
    confidence: float,
) -> Optional[PolicyResult]:
    similar_services = knowledge_base.find_similar_services(message)
    if not similar_services:
        return None

    service_names = ", ".join(service.name for service in similar_services)
    return PolicyResult(
        action=PolicyAction.CLARIFY,
        reason=PolicyReason.SIMILAR_SERVICES_FOUND,
        confidence=confidence,
        safe_context={
            "similar": services_summary(similar_services),
            "message_to_user": f"Такой услуги у нас пока не вижу, но есть похожие: {service_names}. Интересно?",
        },
        quick_actions=[
            {"label": service.name, "type": "message", "value": service.name}
            for service in similar_services
        ],
    )


def cosmetic_concern_services(message: str, knowledge_base: KnowledgeBase) -> list[Any]:
    normalized_message = normalize_text(message)
    service_ids: list[str] = []
    for keyword, mapped_service_ids in COSMETIC_CONCERN_SERVICE_MAP.items():
        if keyword in normalized_message:
            service_ids.extend(mapped_service_ids)

    services = [
        service
        for service_id in service_ids
        if (service := knowledge_base.find_service_by_id(service_id)) is not None
    ]
    if not services:
        services = knowledge_base.find_similar_services(message, threshold=0.45)

    unique_services: list[Any] = []
    seen_ids: set[str] = set()
    for service in services:
        if service.id not in seen_ids:
            unique_services.append(service)
            seen_ids.add(service.id)
    return unique_services[:3]


def mentions_unknown_service(normalized_message: str) -> bool:
    if normalized_message in GENERIC_PRICE_MESSAGES:
        return False
    service_noise = {
        "сколько",
        "стоит",
        "цена",
        "стоимость",
        "прайс",
        "хочу",
        "уточнить",
        "услуга",
        "услугу",
        "процедура",
        "процедуру",
    }
    tokens = [token for token in normalized_message.split() if token not in service_noise]
    return bool(tokens)


def city_prepositional(city: str) -> str:
    """маленькая вспомогательная функция для демо-текста, не полноценная морфология."""

    city_forms = {
        "Москва": "Москве",
        "Санкт-Петербург": "Санкт-Петербурге",
    }
    return city_forms.get(city, city)
