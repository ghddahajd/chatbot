"""Base LLM client interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import Message


class BaseLLMClient(ABC):
    """Abstract LLM client interface."""

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        context: dict[str, Any],
        user_message: str,
        history: list[Message],
    ) -> str:
        """Return a model completion based on provided safe context."""

    async def classify_and_extract(
        self,
        user_message: str,
        known_services: list[dict[str, str]],
    ) -> dict[str, object]:
        """Classify intent and extract a service id."""

        del user_message, known_services
        return {"intent": "service_mention", "service_id": None, "confidence": 0.0}

    async def small_talk(self, company_name: str, user_message: str) -> str:
        """Return a lightweight conversational answer without KB data."""

        del user_message
        return f"Здравствуйте! Я консультант {company_name}. Чем могу помочь по услугам центра?"

    async def service_consultation(
        self,
        context: dict[str, Any],
        user_message: str,
    ) -> str:
        """Return a soft consultation answer for a known service or cosmetic concern."""

        del user_message
        message_to_user = context.get("message_to_user")
        if isinstance(message_to_user, str) and message_to_user.strip():
            return message_to_user.strip()
        return "Понял запрос. Могу подсказать по стоимости или передать вопрос специалисту."

    async def classify_medical_risk(self, user_message: str) -> str:
        """Classify a consultation-zone message as MEDICAL or COSMETIC."""

        del user_message
        return "COSMETIC"

    async def medical_handoff(self, user_message: str) -> str:
        """Return a safe handoff answer for medical-risk consultation-zone messages."""

        del user_message
        return "Понимаю, лучше уточнить это у специалиста напрямую — подключаю оператора."
