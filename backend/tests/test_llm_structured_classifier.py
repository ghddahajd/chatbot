"""проверки structured classifier на уровне llm-клиентов."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from app.llm import MockLLMClient
from app.llm.openai_compatible import OpenAIClient
from app.llm.prompts import build_system_prompt
from app.models import Lead, Message, MessageRole, Session


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


def test_structured_classifier_schema_marks_every_field_required() -> None:
    """Живой репро §2026-08-22: Yandex отклоняет схему 400-й, если хоть одно поле не в
    required (pydantic по умолчанию исключает оттуда всё с дефолтом — тут всё, кроме
    intent/confidence). Проверено живым вызовом на реальном ключе отдельно; здесь —
    что сама схема, которую мы реально отправляем, содержит все поля в required."""

    client = _RecordingOpenAIClient({"intent": "price_question", "confidence": 0.5})

    asyncio.run(
        client.classify_structured("цена на эпиляцию", KNOWN_SERVICES, {"type": "generic_service"})
    )

    schema = client.last_payload["response_format"]["json_schema"]["schema"]
    assert set(schema["required"]) == set(schema["properties"].keys())
    assert "confidence" in schema["required"]
    assert "risk" in schema["required"]


def test_build_system_prompt_uses_question_type_block() -> None:
    price_prompt = build_system_prompt("price")
    faq_prompt = build_system_prompt("faq_question")

    assert "Тип ответа: цена" in price_prompt
    assert "Тип ответа: статья/FAQ" not in price_prompt
    assert "Тип ответа: статья/FAQ" in faq_prompt
    assert "Тип ответа: цена" not in faq_prompt


def test_openai_complete_sends_history_as_chat_turns_not_system_json() -> None:
    client = _RecordingCompletionClient(
        "Чистка лица стоит от 4 500 ₽. Это предварительная стоимость. "
        "Точную сумму подтвердит менеджер после уточнения деталей."
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


def test_summarize_session_prompt_forbids_claiming_contact_was_provided() -> None:
    """Живой баг (Telegram-карточка, 2026-08-24): "Тест Тестов +79999999999" — деторминированный
    extract_name отверг "Тест Тестов" как не настоящее имя (lead.name="Не указано"), но LLM-
    саммари, видя сырой текст сообщения, независимо написало "предоставил имя и телефон" —
    карточка оператору противоречила сама себе. Промпт теперь явно просит не описывать факт
    предоставления контактов (это уже отдельные поля карточки)."""

    client = _RecordingCompletionClient("Клиент хочет записаться на чистку лица.")
    session = Session(company_id="rosh_import_demo")
    session.messages.append(Message(role=MessageRole.USER, text="хочу записаться на чистку лица"))
    session.messages.append(Message(role=MessageRole.USER, text="Тест Тестов +79999999999"))
    lead = Lead(
        company_id="rosh_import_demo",
        session_id="s1",
        name="Не указано",
        phone="+79999999999",
        summary="хочу записаться на чистку лица",
        reason="booking",
    )

    asyncio.run(client.summarize_session(session, lead))

    system_message = client.last_payload["messages"][0]["content"]
    assert "предоставил ли клиент имя и телефон" in system_message
    assert "уже отдельно показано в карточке" in system_message


def test_summarize_session_prompt_asks_to_quote_both_phones_when_client_gave_two() -> None:
    """Живой баг (Telegram-карточка, 2026-08-24): "мой телефон 89261234567 или можно на
    89169876543" — Lead.phone (одна строка на всю модель) хранит только ПЕРВЫЙ номер
    (extract_phone делает .search(), не .findall()), а карточка оператору не показывает
    второй вовсе. Полная переделка модели под список телефонов — инвазивно для последнего
    дня тестирования; вместо этого промпт саммари просит явно процитировать оба номера
    текстом, чтобы оператору не пришлось лезть в историю переписки."""

    client = _RecordingCompletionClient("Клиент оставил два номера телефона: +79261234567 и +79169876543.")
    session = Session(company_id="rosh_import_demo")
    session.messages.append(
        Message(role=MessageRole.USER, text="мой телефон 89261234567 или можно на 89169876543, как удобнее")
    )
    lead = Lead(
        company_id="rosh_import_demo",
        session_id="s1",
        name="Не указано",
        phone="+79261234567",
        summary="оставил контакт",
        reason="commercial_interest",
    )

    asyncio.run(client.summarize_session(session, lead))

    system_message = client.last_payload["messages"][0]["content"]
    assert "процитируй все такие номера" in system_message


def test_summarize_session_prompt_excludes_phone_bot_rejected_as_incomplete() -> None:
    """Живой баг (2026-08-27): "леха 8999343453" (10 цифр, неполный) бот отклонил
    ("Похоже, номер неполный..."), клиент прислал второй, валидный, номер — а LLM-саммари
    всё равно процитировала оба, включая отклонённый ("телефоны: +7 (999) 343-45-3 и
    +7 (495) 000-00-00"), хотя дозвониться по первому нельзя. Промпт теперь явно требует
    не считать отклонённый ботом номер валидным контактом."""

    client = _RecordingCompletionClient("Клиент оставил номер +74950000000.")
    lead = Lead(
        company_id="rosh_import_demo",
        session_id="s1",
        name="Леха",
        phone="+74950000000",
        summary="оставил контакт",
        reason="commercial_interest",
    )
    asyncio.run(client.summarize_session(Session(company_id="rosh_import_demo"), lead))

    system_message = client.last_payload["messages"][0]["content"]
    assert "неполный" in system_message or "некорректный" in system_message
    assert "не указывай его" in system_message


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
