"""LLM provider abstraction."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx


PRICE_DISCLAIMER = "Предварительно так, точную информацию подтвердит специалист."
DEFAULT_FALLBACK = "Уточните, пожалуйста, ваш вопрос, и я постараюсь помочь в рамках услуг центра."
SYSTEM_PROMPT = (
    "Ты консультант медицинского/косметологического центра. Ты не врач и не ставишь диагнозы. "
    "Отвечай только по переданному safe_context. Не придумывай цены, сроки, услуги или рекомендации. "
    "Если данных нет, предложи уточнение или специалиста."
)


class BaseLLMClient(ABC):
    """Abstract LLM client interface."""

    @abstractmethod
    async def complete(self, system_prompt: str, context: dict[str, Any], user_message: str) -> str:
        """Return a model completion based on provided safe context."""


class MockLLMClient(BaseLLMClient):
    """Deterministic response builder used when no external LLM is configured."""

    async def complete(self, system_prompt: str, context: dict[str, Any], user_message: str) -> str:
        del system_prompt, user_message

        message_to_user = context.get("message_to_user")
        if isinstance(message_to_user, str) and message_to_user.strip():
            return message_to_user.strip()

        service = context.get("service") or {}
        price = context.get("price") or {}
        company = context.get("company") or {}
        question_type = context.get("question_type")

        service_name = service.get("name")
        short_description = service.get("short_description")
        duration = service.get("duration")
        working_hours = company.get("working_hours")
        address = company.get("address")

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
        if address:
            parts.append(f"Адрес: {address}.")
        if working_hours:
            parts.append(f"Режим работы: {working_hours}.")

        if parts:
            return " ".join(parts)

        return DEFAULT_FALLBACK


class OpenAIClient(BaseLLMClient):
    """Minimal OpenAI-compatible client."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def complete(self, system_prompt: str, context: dict[str, Any], user_message: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "system", "content": f"safe_context: {context}"},
                {"role": "user", "content": user_message},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=headers,
            )
            response.raise_for_status()

        data = response.json()
        return (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", DEFAULT_FALLBACK)
            .strip()
        )


def build_llm_client(
    provider: str,
    api_key: str,
    model: str,
    base_url: str,
) -> BaseLLMClient:
    """Select the default LLM provider."""

    normalized_provider = provider.lower().strip()
    if normalized_provider == "mock" or not api_key:
        return MockLLMClient()

    if normalized_provider in {"openai", "gemini", "openai_compatible"}:
        return OpenAIClient(
            api_key=api_key,
            model=model,
            base_url=base_url,
        )

    return MockLLMClient()


def get_system_prompt() -> str:
    """Return the fixed system prompt."""

    return SYSTEM_PROMPT
