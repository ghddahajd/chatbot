"""проверки structured classifier на уровне llm-клиентов."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.llm import MockLLMClient
from app.llm.openai_compatible import OpenAIClient


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
            {"type": "generic_service"},
        )
    )

    assert result is not None
    assert result.intent == "regulated_advice"
    assert result.risk == "regulated_advice"


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
