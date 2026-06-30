from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Optional

import httpx

from ..models import Message
from ..validator import fallback_after_invalid_response, validate_consultation_response, validate_response
from .base import BaseLLMClient
from .classification import (
    IntentClassification,
    safe_normalize_intent_classification,
)
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
    STRUCTURED_INTENT_CLASSIFICATION_PROMPT,
)


logger = logging.getLogger(__name__)


def enforce_required_disclaimers(answer: str, context: dict[str, Any]) -> str:
    """держит жёсткие бизнес-правила вне поведения модели."""

    from .prompts import PRICE_DISCLAIMER

    phrasebook = context.get("phrasebook") if isinstance(context.get("phrasebook"), dict) else {}
    price_disclaimer = str(phrasebook.get("price_disclaimer") or PRICE_DISCLAIMER)
    clean_answer = answer.strip() or DEFAULT_FALLBACK
    if context.get("question_type") in {"price", "duration"} and price_disclaimer not in clean_answer:
        return f"{clean_answer} {price_disclaimer}"
    return clean_answer


def enforce_small_talk_pivot(answer: str, company_name: str, user_message: str = "") -> str:
    """делает лёгкий чат полезным, даже если модель ответила слишком коротко."""

    del company_name
    clean_answer = answer.strip()
    if len(clean_answer) < 25:
        return small_talk_template(user_message)
    if "услуг" not in clean_answer.lower() and "цент" not in clean_answer.lower():
        return f"{clean_answer} Могу подсказать по услугам, ценам или записи."
    return clean_answer


class OpenAIClient(BaseLLMClient):
    """минимальный openai-compatible клиент."""

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

    async def _post_chat_completions_with_schema_fallback(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> httpx.Response:
        try:
            return await self._post_chat_completions(payload, headers)
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in {400, 404, 422} or "response_format" not in payload:
                raise
            fallback_payload = dict(payload)
            fallback_payload.pop("response_format", None)
            logger.info(
                "structured_classifier_schema=fallback status=%s model=%s",
                error.response.status_code,
                self.model,
            )
            return await self._post_chat_completions(fallback_payload, headers)

    def _context_for_model(self, context: dict[str, Any]) -> str:
        lines: list[str] = []
        service = context.get("service") if isinstance(context.get("service"), dict) else {}
        price = context.get("price") if isinstance(context.get("price"), dict) else {}
        company = context.get("company") if isinstance(context.get("company"), dict) else {}
        question_type = context.get("question_type")

        if question_type == "list_services":
            all_services = context.get("all_services")
            if isinstance(all_services, list):
                names = [
                    str(service_item.get("name"))
                    for service_item in all_services
                    if isinstance(service_item, dict) and service_item.get("name")
                ]
                if names:
                    lines.append("Услуги центра: " + ", ".join(names))
            lines.append("Тип вопроса: list_services")
            return "\n".join(lines) or "Нет списка услуг."

        if question_type == "explanation":
            if service.get("name"):
                lines.append(f"Услуга: {service['name']}")
            if service.get("short_description"):
                lines.append(f"Описание: {service['short_description']}")
            lines.append("Тип вопроса: explanation")
            return "\n".join(lines) or "Нет описания услуги."

        if service.get("name"):
            lines.append(f"Услуга: {service['name']}")
        if service.get("short_description"):
            lines.append(f"Описание: {service['short_description']}")
        if price.get("price_text"):
            lines.append(f"Стоимость: {price['price_text']}")
        if service.get("duration"):
            lines.append(f"Длительность: {service['duration']}")
        if company.get("working_hours"):
            lines.append(f"Режим работы: {company['working_hours']}")
        if company.get("address"):
            lines.append(f"Адрес: {company['address']}")
        if question_type:
            lines.append(f"Тип вопроса: {question_type}")

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

    def _consultation_history_for_model(self, history: Optional[list[Message]]) -> str:
        lines: list[str] = []
        for message in (history or [])[-6:]:
            if message.role.value not in {"user", "assistant"}:
                continue
            role = "Клиент" if message.role.value == "user" else "Ассистент"
            text = message.text.strip()
            if text:
                lines.append(f"{role}: {text}")
        return "\n".join(lines) or "Истории нет."

    async def complete(
        self,
        system_prompt: str,
        context: dict[str, Any],
        user_message: str,
        history: list[Message],
    ) -> str:
        context_for_model = self._context_for_model(context)
        phrasebook = context.get("phrasebook") if isinstance(context.get("phrasebook"), dict) else {}
        price_disclaimer = str(phrasebook.get("price_disclaimer") or "")
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
                        f"Клиентская оговорка цены/сроков: {price_disclaimer}\n\n"
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
            "max_tokens": 180,
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
        if not validate_response(answer, context):
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

    def _structured_classifier_user_payload(
        self,
        user_message: str,
        known_services: list[dict[str, str]],
        domain_profile: dict[str, Any] | None,
    ) -> str:
        services_payload = json.dumps(known_services, ensure_ascii=False, default=str)
        profile_payload = json.dumps(domain_profile or {}, ensure_ascii=False, default=str)
        return (
            f"known_services JSON:\n{services_payload}\n\n"
            f"domain_profile JSON:\n{profile_payload}\n\n"
            f"user_message:\n{user_message}"
        )

    def _structured_classifier_response_format(self) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "intent_classification",
                "strict": False,
                "schema": IntentClassification.model_json_schema(),
            },
        }

    async def classify_structured(
        self,
        user_message: str,
        known_services: list[dict[str, str]],
        domain_profile: dict[str, Any] | None = None,
    ) -> IntentClassification | None:
        known_service_ids = {
            str(service.get("id"))
            for service in known_services
            if service.get("id")
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": STRUCTURED_INTENT_CLASSIFICATION_PROMPT},
                {
                    "role": "user",
                    "content": self._structured_classifier_user_payload(
                        user_message,
                        known_services,
                        domain_profile,
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 220,
            "response_format": self._structured_classifier_response_format(),
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            response = await asyncio.wait_for(
                self._post_chat_completions_with_schema_fallback(payload, headers),
                timeout=min(self.timeout, 6.0),
            )
        except Exception as error:
            logger.info("structured_classifier_source=fallback reason=request_error error=%s", type(error).__name__)
            return None

        data = response.json()
        content = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "{}")
            .strip()
        )
        raw_result = tolerant_json_parse(content)
        if raw_result is None:
            logger.info("structured_classifier_source=fallback reason=invalid_json")
            return None

        result = safe_normalize_intent_classification(raw_result, known_service_ids)
        if result is None:
            logger.info("structured_classifier_source=fallback reason=schema_validation")
            return None
        logger.info(
            "structured_classifier_source=provider intent=%s risk=%s service_id=%s confidence=%.2f",
            result.intent,
            result.risk,
            result.service_id,
            result.confidence,
        )
        return result

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
        history: Optional[list[Message]] = None,
    ) -> str:
        context_for_model = self._consultation_context_for_model(context)
        history_for_model = self._consultation_history_for_model(history)
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
                        f"Короткая история диалога:\n{history_for_model}\n\n"
                        f"Сообщение пользователя: {user_message}"
                    ),
                },
            ],
            "temperature": 0.45 if self.disable_thinking else 0.72,
            "max_tokens": 120,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            response = await asyncio.wait_for(
                self._post_chat_completions(payload, headers),
                timeout=min(self.timeout, 8.0),
            )
        except Exception as error:
            logger.warning("service_consultation_source=fallback reason=request_error error=%s", type(error).__name__)
            return service_consultation_template(context, user_message, history)

        data = response.json()
        answer = (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
        if not validate_consultation_response(answer, context):
            logger.warning("service_consultation_source=fallback reason=validator answer=%r", answer[:240])
            return service_consultation_template(context, user_message, history)
        logger.info("service_consultation_source=provider model=%s", self.model)
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
        except Exception as error:
            logger.warning("medical_classifier_source=fallback reason=request_error error=%s", type(error).__name__)
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
        except Exception as error:
            logger.warning("medical_handoff_source=fallback reason=request_error error=%s", type(error).__name__)
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
