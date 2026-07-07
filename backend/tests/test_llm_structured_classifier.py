"""проверки structured classifier на уровне llm-клиентов."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.llm import MockLLMClient
from app.llm.openai_compatible import OpenAIClient
from app.llm.prompts import build_system_prompt
from app.models import Message, MessageRole


KNOWN_SERVICES = [
    {"id": "facial_cleansing", "name": "Чистка лица", "synonyms": ["чистка"]},
    {"id": "laser_epilation", "name": "Лазерная эпиляция", "synonyms": ["эпиляция"]},
]


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


class _RecordingOpenAIClient(OpenAIClient):
    def __init__(self, response_content: dict[str, Any]) -> None:
        super().__init__(api_key="test-key", model="test-model", base_url="http://test")
        self.response_content = response_content
        self.last_payload: dict[str, Any] | None = None

    async def _post_chat_completions_with_schema_fallback(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> _FakeResponse:
        del headers
        self.last_payload = payload
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(self.response_content, ensure_ascii=False)
                        }
                    }
                ]
            }
        )


class _RecordingCompletionClient(OpenAIClient):
    def __init__(self, answer: str) -> None:
        super().__init__(api_key="test-key", model="test-model", base_url="http://test")
        self.answer = answer
        self.last_payload: dict[str, Any] | None = None

    async def _post_chat_completions(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> _FakeResponse:
        del headers
        self.last_payload = payload
        return _FakeResponse({"choices": [{"message": {"content": self.answer}}]})


def test_mock_structured_classifier_returns_contract() -> None:
    client = MockLLMClient()

    result = asyncio.run(
        client.classify_structured(
            "сколько стоит чистка лица",
            KNOWN_SERVICES,
            {"type": "generic_service"},
        )
    )

    assert result is not None
    assert result.intent == "price_question"
    assert result.service_id == "facial_cleansing"
    assert result.service_match_type == "exact"


def test_mock_structured_classifier_maps_medical_to_regulated_risk() -> None:
    client = MockLLMClient()

    result = asyncio.run(
        client.classify_structured(
            "у меня воспаление что делать",
            KNOWN_SERVICES,
            {"type": "medical", "restricted_advice": ["medical_treatment"]},
        )
    )

    assert result is not None
    assert result.intent == "regulated_advice"
    assert result.risk == "regulated_advice"


def test_mock_complete_varies_default_price_disclaimer() -> None:
    client = MockLLMClient()
    context = {
        "question_type": "price",
        "service": {"name": "Чистка лица", "short_description": "Описание услуги"},
        "price": {"price_text": "от 4 500 ₽"},
    }

    answers = {
        asyncio.run(client.complete("", context, message, []))
        for message in [
            "сколько стоит чистка лица",
            "цена чистки лица",
            "сколько по стоимости чистка лица",
            "подскажите стоимость чистки лица",
            "сколько будет чистка лица",
        ]
    }

    assert len(answers) > 1


def test_openai_structured_classifier_uses_json_schema_response_format() -> None:
    client = _RecordingOpenAIClient(
        {
            "intent": "price_question",
            "risk": "safe",
            "service_id": "laser_epilation",
            "service_match_type": "exact",
            "confidence": 0.91,
            "reason_code": "price_requested",
        }
    )

    result = asyncio.run(
        client.classify_structured(
            "цена на эпиляцию",
            KNOWN_SERVICES,
            {"type": "generic_service", "restricted_advice": ["medical_treatment"]},
        )
    )

    assert result is not None
    assert result.intent == "price_question"
    assert result.service_id == "laser_epilation"
    assert client.last_payload is not None
    assert client.last_payload["response_format"]["type"] == "json_schema"
    assert "domain_profile JSON" in client.last_payload["messages"][1]["content"]


def test_build_system_prompt_uses_question_type_block() -> None:
    price_prompt = build_system_prompt("price")
    faq_prompt = build_system_prompt("faq_question")

    assert "Тип ответа: цена" in price_prompt
    assert "Тип ответа: статья/FAQ" not in price_prompt
    assert "Тип ответа: статья/FAQ" in faq_prompt
    assert "Тип ответа: цена" not in faq_prompt


def test_openai_complete_sends_history_as_chat_turns_not_system_json() -> None:
    client = _RecordingCompletionClient(
        "Чистка лица стоит от 4 500 ₽. Это предварительно, точнее подскажет менеджер после уточнения деталей."
    )
    context = {
        "question_type": "price",
        "service": {"name": "Чистка лица", "short_description": "Описание услуги"},
        "price": {"price_text": "от 4 500 ₽"},
    }
    history = [
        Message(role=MessageRole.USER, text="хочу чистку лица"),
        Message(role=MessageRole.ASSISTANT, text="Могу подсказать цену."),
    ]

    answer = asyncio.run(client.complete("", context, "а сколько?", history))

    assert "4 500" in answer
    assert client.last_payload is not None
    messages = client.last_payload["messages"]
    system_message = messages[0]["content"]
    assert "recent_history JSON" not in system_message
    assert "хочу чистку лица" not in system_message
    assert {"role": "user", "content": "хочу чистку лица"} in messages
    assert {"role": "assistant", "content": "Могу подсказать цену."} in messages
    assert messages[-1]["role"] == "user"
    assert client.last_payload["max_tokens"] == 320
    assert "Тип ответа: цена" in system_message


def test_openai_structured_classifier_returns_none_on_invalid_schema() -> None:
    client = _RecordingOpenAIClient(
        {
            "intent": "unsupported_new_intent",
            "risk": "safe",
            "service_match_type": "none",
            "confidence": 0.2,
        }
    )

    result = asyncio.run(
        client.classify_structured(
            "непонятный запрос",
            KNOWN_SERVICES,
            {"type": "generic_service"},
        )
    )

    assert result is None
