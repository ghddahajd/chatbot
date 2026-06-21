"""Deterministic mock LLM client."""

from __future__ import annotations

from typing import Any

from ..knowledge import normalize_text
from ..models import Message
from .base import BaseLLMClient
from .prompts import DEFAULT_FALLBACK, PRICE_DISCLAIMER, SMALL_TALK_SERVICE_PIVOT


def small_talk_template(user_message: str) -> str:
    normalized_message = normalize_text(user_message)

    if "спасибо" in normalized_message or "благодарю" in normalized_message:
        return f"Пожалуйста. Если нужно — {SMALL_TALK_SERVICE_PIVOT.lower()}"

    if any(greeting in normalized_message.split() for greeting in {"привет", "здравствуй", "хай", "ку"}):
        return f"Здравствуйте! Я на связи. {SMALL_TALK_SERVICE_PIVOT}"

    if (
        "как дела" in normalized_message
        or "что делаешь" in normalized_message
        or "чем занимаешься" in normalized_message
    ):
        return "Я на связи и могу помочь по теме центра: услуги, цены, запись или специалист."

    return f"Я здесь, чтобы помочь по услугам центра. {SMALL_TALK_SERVICE_PIVOT}"


class MockLLMClient(BaseLLMClient):
    """Deterministic response builder used when no external LLM is configured."""

    async def complete(
        self,
        system_prompt: str,
        context: dict[str, Any],
        user_message: str,
        history: list[Message],
    ) -> str:
        del system_prompt, user_message, history

        message_to_user = context.get("message_to_user")
        if isinstance(message_to_user, str) and message_to_user.strip():
            return message_to_user.strip()

        service = context.get("service") or {}
        price = context.get("price") or {}
        company = context.get("company") or {}
        question_type = context.get("question_type")
        all_services = context.get("all_services")
        suggested_services = context.get("suggested_services")

        if question_type == "list_services" and isinstance(all_services, list):
            service_names = [
                str(service.get("name"))
                for service in all_services
                if isinstance(service, dict) and service.get("name")
            ]
            if service_names:
                return "В центре доступны услуги: " + ", ".join(service_names) + ". Какая услуга вас интересует?"

        if question_type == "cosmetic_concern" and isinstance(suggested_services, list):
            service_names = [
                str(service.get("name"))
                for service in suggested_services
                if isinstance(service, dict) and service.get("name")
            ]
            if service_names:
                return (
                    "Для такого запроса обычно подходят: "
                    + ", ".join(service_names)
                    + ". Точные рекомендации даст специалист на консультации."
                )

        service_name = service.get("name")
        short_description = service.get("short_description")
        duration = service.get("duration")

        parts: list[str] = []
        if service_name:
            parts.append(f"{service_name} — {short_description}")
        if question_type == "duration":
            if duration:
                parts.append(f"Длительность: {duration}. {PRICE_DISCLAIMER}")
            else:
                parts.append("Точную длительность уточнит специалист.")
        elif price.get("price_text"):
            parts.append(f"Стоимость: {price['price_text']}. {PRICE_DISCLAIMER}")
        elif service_name:
            parts.append("Точные детали по стоимости и длительности уточнит специалист.")

        if parts:
            return " ".join(parts)

        return DEFAULT_FALLBACK

    async def small_talk(self, company_name: str, user_message: str) -> str:
        del company_name
        return small_talk_template(user_message)

    async def classify_and_extract(
        self,
        user_message: str,
        known_services: list[dict[str, str]],
    ) -> dict[str, object]:
        from ..policy import classify_and_extract

        return classify_and_extract(user_message, known_services)
