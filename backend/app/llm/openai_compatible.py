"""OpenAI-compatible LLM client, including local Ollama."""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any

import httpx

from ..models import Message
from ..validator import fallback_after_invalid_response, validate_consultation_response, validate_response
from .base import BaseLLMClient
from .mock import service_consultation_template, small_talk_template
from .parsing import normalize_classification_result, tolerant_json_parse
from .prompts import (
    DEFAULT_FALLBACK,
    INTENT_CLASSIFICATION_PROMPT,
    MEDICAL_HANDOFF_FALLBACK,
    MEDICAL_HANDOFF_PROMPT,
    MEDICAL_RISK_CLASSIFICATION_PROMPT,
    SERVICE_CONSULTATION_PROMPT,
    SERVICE_CONSULTATION_TONE_VARIANTS,
    SMALL_TALK_PROMPT,
)


def enforce_required_disclaimers(answer: str, context: dict[str, Any]) -> str:
    """Keep hard business rules outside model behavior."""

    from .prompts import PRICE_DISCLAIMER

    clean_answer = answer.strip() or DEFAULT_FALLBACK
    if context.get("question_type") in {"price", "duration"} and PRICE_DISCLAIMER not in clean_answer:
        return f"{clean_answer} {PRICE_DISCLAIMER}"
    return clean_answer


def enforce_small_talk_pivot(answer: str, company_name: str, user_message: str = "") -> str:
    """Make lightweight chat useful even if the model replies too briefly."""

    del company_name
    clean_answer = answer.strip()
    if len(clean_answer) < 25:
        return small_talk_template(user_message)
    if "услуг" not in clean_answer.lower() and "цент" not in clean_answer.lower():
        return f"{clean_answer} Могу подсказать по услугам, ценам или записи."
    return clean_answer


class OpenAIClient(BaseLLMClient):
    """Minimal OpenAI-compatible client."""

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 30.0,
        disable_thinking: bool = False,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.disable_thinking = disable_thinking

    async def _post_chat_completions(self, payload: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
        request_payload = dict(payload)
        if self.disable_thinking:
            request_payload["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=request_payload,
                headers=headers,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as error:
                if self.disable_thinking and error.response.status_code in {400, 404, 422}:
                    response = await client.post(
                        f"{self.base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                    )
                    response.raise_for_status()
                    return response
                raise
            return response

    def _context_for_model(self, context: dict[str, Any]) -> str:
        lines: list[str] = []
        service = context.get("service") if isinstance(context.get("service"), dict) else {}
        price = context.get("price") if isinstance(context.get("price"), dict) else {}
        company = context.get("company") if isinstance(context.get("company"), dict) else {}

        if service.get("name"):
            lines.append(f"Услуга: {service['name']}")
        if service.get("short_description"):
            lines.append(f"Описание: {service['short_description']}")
        if price.get("price_text"):
            lines.append(f"Стоимость: {price['price_text']}")
        if company.get("working_hours"):
            lines.append(f"Режим работы: {company['working_hours']}")
        if company.get("address"):
            lines.append(f"Адрес: {company['address']}")
        if context.get("question_type"):
            lines.append(f"Тип вопроса: {context['question_type']}")

        suggested_services = context.get("suggested_services")
        if isinstance(suggested_services, list) and suggested_services:
            names = [
                str(service_item.get("name"))
                for service_item in suggested_services
                if isinstance(service_item, dict) and service_item.get("name")
            ]
            if names:
                lines.append("Подходящие услуги: " + ", ".join(names))

        message_to_user = context.get("message_to_user")
        if isinstance(message_to_user, str) and message_to_user.strip():
            lines.append(f"Готовый безопасный смысл ответа: {message_to_user.strip()}")

        return "\n".join(lines) or "Нет дополнительных данных. Нужно мягко уточнить запрос."

    def _consultation_context_for_model(self, context: dict[str, Any]) -> str:
        lines: list[str] = []
        service = context.get("service") if isinstance(context.get("service"), dict) else {}
        if service.get("name"):
            lines.append(f"Услуга: {service['name']}")
        if service.get("short_description"):
            lines.append(f"Описание: {service['short_description']}")

        suggested_services = context.get("suggested_services")
        if isinstance(suggested_services, list) and suggested_services:
            names = [
                str(service_item.get("name"))
                for service_item in suggested_services
                if isinstance(service_item, dict) and service_item.get("name")
            ]
            if names:
                lines.append("Близкие направления: " + ", ".join(names))

        return "\n".join(lines) or "Есть только общий косметологический запрос без точной услуги."

    async def complete(
        self,
        system_prompt: str,
        context: dict[str, Any],
        user_message: str,
        history: list[Message],
    ) -> str:
        context_for_model = self._context_for_model(context)
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
                        f"Контекст для ответа:\n{context_for_model}\n\n"
                        f"recent_history JSON:\n{history_payload}"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Сформулируй один готовый ответ клиенту только на основе контекста выше. "
                        "Не показывай служебные поля, JSON, списки ключей или внутренние данные. "
                        f"Тип вопроса: {context.get('question_type', 'general')}. "
                        f"Сообщение клиента для тональности: {user_message}"
                    ),
                },
            ],
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        response = await self._post_chat_completions(payload, headers)

        data = response.json()
        answer = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", DEFAULT_FALLBACK)
            .strip()
        )
        answer = enforce_required_disclaimers(answer, context)
        if not validate_response(answer):
            return fallback_after_invalid_response(answer, context)
        return answer

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

        response = await self._post_chat_completions(payload, headers)

        data = response.json()
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "{}")
            .strip()
        )
        raw_result = tolerant_json_parse(content)
        if raw_result is None:
            return {"intent": "service_mention", "service_id": None, "confidence": 0.0}
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

        response = await self._post_chat_completions(payload, headers)

        data = response.json()
        answer = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", DEFAULT_FALLBACK)
            .strip()
        )
        return enforce_small_talk_pivot(answer, company_name, user_message)

    async def service_consultation(
        self,
        context: dict[str, Any],
        user_message: str,
    ) -> str:
        context_for_model = self._consultation_context_for_model(context)
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        SERVICE_CONSULTATION_PROMPT
                        + "\n"
                        + random.choice(SERVICE_CONSULTATION_TONE_VARIANTS)
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"Контекст найденной услуги:\n{context_for_model}\n\n"
                        f"Сообщение пользователя: {user_message}"
                    ),
                },
            ],
            "temperature": 0.55,
            "max_tokens": 90,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            response = await asyncio.wait_for(
                self._post_chat_completions(payload, headers),
                timeout=min(self.timeout, 8.0),
            )
        except Exception:
            return service_consultation_template(context, user_message)

        data = response.json()
        answer = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not validate_consultation_response(answer):
            return service_consultation_template(context, user_message)
        return answer

    async def classify_medical_risk(self, user_message: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": MEDICAL_RISK_CLASSIFICATION_PROMPT},
                {"role": "user", "content": f"Сообщение: {user_message}"},
            ],
            "temperature": 0,
            "max_tokens": 4,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            response = await asyncio.wait_for(
                self._post_chat_completions(payload, headers),
                timeout=min(self.timeout, 5.0),
            )
        except Exception:
            return "MEDICAL"

        data = response.json()
        answer = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
            .upper()
        )
        if "COSMETIC" in answer and "MEDICAL" not in answer:
            return "COSMETIC"
        if "MEDICAL" in answer:
            return "MEDICAL"
        return "MEDICAL"

    async def medical_handoff(self, user_message: str) -> str:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": MEDICAL_HANDOFF_PROMPT},
                {"role": "user", "content": f"Сообщение: {user_message}"},
            ],
            "temperature": 0.35,
            "max_tokens": 70,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            response = await asyncio.wait_for(
                self._post_chat_completions(payload, headers),
                timeout=min(self.timeout, 6.0),
            )
        except Exception:
            return MEDICAL_HANDOFF_FALLBACK

        data = response.json()
        answer = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not validate_consultation_response(answer):
            return MEDICAL_HANDOFF_FALLBACK
        return answer
