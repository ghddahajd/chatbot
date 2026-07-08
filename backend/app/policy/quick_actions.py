"""сборщики быстрых действий для политики."""

from __future__ import annotations

from typing import Any

from ..knowledge import KnowledgeBase


def _dedupe_services_by_name(services: list[Any]) -> list[Any]:
    unique_services: list[Any] = []
    seen_names: set[str] = set()
    for service in services:
        name = str(getattr(service, "name", "") or "").strip().lower().replace("ё", "е")
        if not name or name in seen_names:
            continue
        unique_services.append(service)
        seen_names.add(name)
    return unique_services


def all_services_context(knowledge_base: KnowledgeBase) -> list[dict[str, str]]:
    return [
        {
            "name": service.name,
            "category": service.category,
            "short_description": service.short_description,
        }
        for service in _dedupe_services_by_name(knowledge_base.services)
    ]


def service_name_quick_actions(knowledge_base: KnowledgeBase) -> list[dict[str, str]]:
    services = knowledge_base.services[:4] if len(knowledge_base.services) > 5 else knowledge_base.services
    actions = [
        {
            "label": service.name,
            "type": "message",
            "value": service.name,
        }
        for service in services
    ]
    actions.append(
        {
            "label": "Посмотреть все услуги",
            "type": "message",
            "value": "покажи услуги",
        }
    )
    return actions


def services_summary(services: list[Any]) -> list[dict[str, str]]:
    return [
        {
            "id": service.id,
            "name": service.name,
            "short_description": service.short_description,
        }
        for service in _dedupe_services_by_name(services)
    ]
