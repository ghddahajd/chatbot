"""LLM provider abstraction."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx


PRICE_DISCLAIMER = "Предварительно так, точнее сообщит специалист."
DEFAULT_FALLBACK = "Уточните, пожалуйста, ваш вопрос, и я постараюсь помочь в рамках услуг центра."
SYSTEM_PROMPT = (
    "Ты консультант медицинского/косметологического центра. Ты не врач и не ставишь диагнозы. "
    "Отвечай только по переданному safe_context. Не придумывай цены, сроки, услуги или рекомендации. "
    "Если данных нет, предложи уточнение или специалиста. "
    f"Если отвечаешь про цену или длительность, обязательно добавь фразу: {PRICE_DISCLAIMER} "
    "Отвечай кратко, по-русски, без Markdown."
)
INTENT_CLASSIFICATION_PROMPT = (
    "Классифицируй сообщение пользователя в одну категорию:\n"
    "small_talk — приветствие, благодарность, общая болтовня\n"
    "off_topic — вопрос не связан с медициной/косметологией центра\n"
    "list_services — пользователь хочет увидеть список/перечень услуг центра "
    "(любая формулировка: 'покажи услуги', 'а можно услуги', 'что у вас есть', "
    "'хочу глянуть прайс', 'список процедур')\n"
    "in_domain — вопрос про услуги, цены, запись, медицину, оператора\n"
    "Ответь ТОЛЬКО одним словом: small_talk, off_topic, list_services или in_domain."
)
SMALL_TALK_PROMPT = (
    "Ты вежливый консультант медицинского/косметологического центра. "
    "Ответь на приветствие или короткую реплику живо, в 1-2 предложениях, "
    "и мягко спроси, чем помочь по услугам центра. Не упоминай цены и услуги."
)


class BaseLLMClient(ABC):
    """Abstract LLM client interface."""

    @abstractmethod
    async def complete(self, system_prompt: str, context: dict[str, Any], user_message: str) -> str:
        """Return a model completion based on provided safe context."""

    async def classify_intent(self, user_message: str) -> str:
        """Classify message intent without business context."""

        del user_message
        return "in_domain"

    async def small_talk(self, company_name: str, user_message: str) -> str:
        """Return a lightweight conversational answer without KB data."""

        del user_message
        return f"Здравствуйте! Я консультант {company_name}. Чем могу помочь по услугам центра?"


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

    async def small_talk(self, company_name: str, user_message: str) -> str:
        del user_message
        return f"Здравствуйте! Я консультант {company_name}. Чем могу помочь по услугам центра?"


def enforce_required_disclaimers(answer: str, context: dict[str, Any]) -> str:
    """Keep hard business rules outside model behavior."""

    clean_answer = answer.strip() or DEFAULT_FALLBACK
    if context.get("question_type") in {"price", "duration"} and PRICE_DISCLAIMER not in clean_answer:
        return f"{clean_answer} {PRICE_DISCLAIMER}"
    return clean_answer


def enforce_small_talk_pivot(answer: str, company_name: str) -> str:
    """Make lightweight chat useful even if the model replies too briefly."""

    clean_answer = answer.strip()
    pivot = "Чем могу помочь по услугам центра?"
    if len(clean_answer) < 25:
        return f"Здравствуйте! Я консультант {company_name}. {pivot}"
    if "услуг" not in clean_answer.lower() and "цент" not in clean_answer.lower():
        return f"{clean_answer} {pivot}"
    return clean_answer


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
        safe_context = json.dumps(context, ensure_ascii=False, default=str)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": f"{system_prompt}\n\nsafe_context JSON:\n{safe_context}",
                },
                {
                    "role": "user",
                    "content": (
                        "Сформулируй ответ клиенту только на основе safe_context. "
                        f"Тип вопроса: {context.get('question_type', 'general')}. "
                        f"Сообщение клиента для тональности: {user_message}"
                    ),
                },
            ],
            "temperature": 0.2,
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
        answer = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", DEFAULT_FALLBACK)
            .strip()
        )
        return enforce_required_disclaimers(answer, context)

    async def classify_intent(self, user_message: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": INTENT_CLASSIFICATION_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0,
            "max_tokens": 8,
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
        intent = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "in_domain")
            .strip()
            .lower()
        )
        return intent if intent in {"small_talk", "off_topic", "list_services", "in_domain"} else "in_domain"

    async def small_talk(self, company_name: str, user_message: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SMALL_TALK_PROMPT},
                {
                    "role": "user",
                    "content": f"Центр: {company_name}\nСообщение пользователя: {user_message}",
                },
            ],
            "temperature": 0.4,
            "max_tokens": 80,
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
        answer = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", DEFAULT_FALLBACK)
            .strip()
        )
        return enforce_small_talk_pivot(answer, company_name)


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
