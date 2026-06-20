"""LLM provider abstraction."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, Optional

import httpx

from .models import Message


PRICE_DISCLAIMER = "Предварительно так, точнее сообщит специалист."
DEFAULT_FALLBACK = "Уточните, пожалуйста, ваш вопрос, и я постараюсь помочь в рамках услуг центра."
SYSTEM_PROMPT = (
    "Ты консультант медицинского/косметологического центра. Ты не врач и не ставишь диагнозы. "
    "Отвечай только по переданному safe_context. Не придумывай цены, сроки, услуги или рекомендации. "
    "Если данных нет, предложи уточнение или специалиста. "
    "Учитывай контекст предыдущих сообщений в этом диалоге. "
    "Если пользователь продолжает тему — не переспрашивай то что уже сказал. "
    f"Если отвечаешь про цену или длительность, обязательно добавь фразу: {PRICE_DISCLAIMER} "
    "Отвечай кратко, по-русски, без Markdown."
)
INTENT_CLASSIFICATION_PROMPT = (
    "Тебе дан список услуг центра: {services}.\n"
    "Проанализируй сообщение пользователя и верни JSON:\n"
    "{\n"
    '  "intent": "small_talk|off_topic|list_services|price_question|'
    'cosmetic_concern|medical_advice|operator_request|service_mention|unknown_service",\n'
    '  "service_id": "<id из списка или null>",\n'
    '  "confidence": 0.0-1.0\n'
    "}\n\n"
    "ВАЖНО:\n"
    "- Если в сообщении одновременно приветствие И конкретный запрос "
    "(услуга/цена/список) — классифицируй по запросу, не по приветствию.\n"
    "- service_id матчи по смыслу, учитывай падежи и склонения "
    "('чистку лица' = 'чистка лица', тот же service_id).\n"
    "- Если упомянута услуга которой точно нет в списке — service_id: null, "
    "intent: unknown_service.\n"
    "- cosmetic_concern — эстетическая жалоба на внешний вид кожи/волос "
    "(НЕ боль, НЕ болезнь, НЕ медицинский симптом).\n"
    "Ответь ТОЛЬКО JSON, без текста вокруг."
)
SMALL_TALK_PROMPT = (
    "Ты вежливый консультант медицинского/косметологического центра. "
    "Ответь на приветствие или короткую реплику живо, в 1-2 предложениях, "
    "и мягко спроси, чем помочь по услугам центра. Не упоминай цены и услуги."
)


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

    async def classify_and_extract(
        self,
        user_message: str,
        known_services: list[dict[str, str]],
    ) -> dict[str, object]:
        from .policy import classify_and_extract

        return classify_and_extract(user_message, known_services)


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


def normalize_classification_result(
    raw_result: dict[str, Any],
    known_services: list[dict[str, str]],
) -> dict[str, object]:
    """Validate model JSON so policy only sees supported values."""

    allowed_intents = {
        "small_talk",
        "off_topic",
        "list_services",
        "price_question",
        "cosmetic_concern",
        "medical_advice",
        "operator_request",
        "service_mention",
        "unknown_service",
    }
    known_service_ids = {str(service.get("id")) for service in known_services}

    intent = str(raw_result.get("intent") or "service_mention").strip().lower()
    if intent not in allowed_intents:
        intent = "service_mention"

    service_id = raw_result.get("service_id")
    if service_id is not None:
        service_id = str(service_id).strip()
    if not service_id or service_id == "null" or service_id not in known_service_ids:
        service_id = None

    try:
        confidence = float(raw_result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    return {"intent": intent, "service_id": service_id, "confidence": confidence}


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

    async def complete(
        self,
        system_prompt: str,
        context: dict[str, Any],
        user_message: str,
        history: list[Message],
    ) -> str:
        safe_context = json.dumps(context, ensure_ascii=False, default=str)
        history_payload = json.dumps(
            [
                {
                    "role": message.role.value,
                    "text": message.text,
                }
                for message in history[-8:]
            ],
            ensure_ascii=False,
            default=str,
        )
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n\n"
                        f"safe_context JSON:\n{safe_context}\n\n"
                        f"recent_history JSON:\n{history_payload}"
                    ),
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

    async def classify_and_extract(
        self,
        user_message: str,
        known_services: list[dict[str, str]],
    ) -> dict[str, object]:
        services_payload = json.dumps(known_services, ensure_ascii=False, default=str)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": INTENT_CLASSIFICATION_PROMPT.replace("{services}", services_payload),
                },
                {"role": "user", "content": user_message},
            ],
            "temperature": 0,
            "max_tokens": 120,
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
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "{}")
            .strip()
        )
        try:
            raw_result = json.loads(content)
        except json.JSONDecodeError:
            start = content.find("{")
            end = content.rfind("}")
            if start >= 0 and end > start:
                try:
                    raw_result = json.loads(content[start : end + 1])
                except json.JSONDecodeError:
                    raw_result = {}
            else:
                raw_result = {}
        return normalize_classification_result(raw_result, known_services)

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
