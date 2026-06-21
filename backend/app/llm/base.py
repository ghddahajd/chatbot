from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from ..models import Message


class BaseLLMClient(ABC):
    """абстрактный интерфейс llm-клиента."""

    @abstractmethod
    async def complete(
        self,
        system_prompt: str,
        context: dict[str, Any],
        user_message: str,
        history: list[Message],
    ) -> str:
        """возвращает ответ модели на основе переданного безопасного контекста."""

    async def classify_and_extract(
        self,
        user_message: str,
        known_services: list[dict[str, str]],
    ) -> dict[str, object]:
        """классифицирует намерение и извлекает id услуги."""

        del user_message, known_services
        return {"intent": "service_mention", "service_id": None, "confidence": 0.0}

    async def small_talk(self, company_name: str, user_message: str) -> str:
        """возвращает лёгкий разговорный ответ без данных базы знаний."""

        del user_message
        return f"Здравствуйте! Я консультант {company_name}. Чем могу помочь по услугам центра?"

    async def service_consultation(
        self,
        context: dict[str, Any],
        user_message: str,
        history: Optional[list[Message]] = None,
    ) -> str:
        """возвращает мягкий консультационный ответ по известной услуге или косметическому запросу."""

        del history
        del user_message
        message_to_user = context.get("message_to_user")
        if isinstance(message_to_user, str) and message_to_user.strip():
            return message_to_user.strip()
        return "Понял запрос. Могу подсказать по стоимости или передать вопрос специалисту."

    async def classify_medical_risk(self, user_message: str) -> str:
        """классифицирует сообщение консультационной зоны как медицинское или косметическое."""

        del user_message
        return "COSMETIC"

    async def medical_handoff(self, user_message: str) -> str:
        """возвращает безопасный ответ для передачи медицинских сообщений специалисту."""

        del user_message
        return "Понимаю, лучше уточнить это у специалиста напрямую — подключаю оператора."
