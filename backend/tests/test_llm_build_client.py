"""проверки build_llm_client — выбор провайдера, до сегодняшнего дня без единого теста."""

from __future__ import annotations

from app.llm import build_llm_client
from app.llm.mock import MockLLMClient
from app.llm.openai_compatible import OpenAIClient


def test_build_llm_client_mock_provider_returns_mock() -> None:
    client = build_llm_client(provider="mock", api_key="unused", model="m", base_url="https://x")
    assert isinstance(client, MockLLMClient)


def test_build_llm_client_missing_api_key_returns_mock() -> None:
    """живой репро-класс (аудит §2026-08-22): именно так LLM_PROVIDER=yandex молча уходил в
    mock раньше — но это конкретное поведение (нет ключа → mock, для любого provider) осталось
    и должно остаться, отдельно от того бага (провайдер не распознавался вообще)."""

    client = build_llm_client(provider="openai", api_key="", model="m", base_url="https://x")
    assert isinstance(client, MockLLMClient)


def test_build_llm_client_openai_returns_openai_client() -> None:
    client = build_llm_client(provider="openai", api_key="k", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
    assert isinstance(client, OpenAIClient)
    assert client.disable_thinking is False


def test_build_llm_client_gemini_returns_openai_client() -> None:
    client = build_llm_client(provider="gemini", api_key="k", model="gemini-3.5-flash", base_url="https://x")
    assert isinstance(client, OpenAIClient)
    assert client.disable_thinking is False


def test_build_llm_client_openai_compatible_disables_thinking() -> None:
    """только этот provider — костыль под локальные thinking-модели (Ollama/qwen)."""

    client = build_llm_client(provider="openai_compatible", api_key="local", model="qwen2.5:3b", base_url="http://x")
    assert isinstance(client, OpenAIClient)
    assert client.disable_thinking is True


def test_build_llm_client_yandex_returns_openai_client_without_disabling_thinking() -> None:
    """живой репро (аудит §2026-08-22): раньше "yandex" вообще не был в наборе признанных
    провайдеров — build_llm_client() молча возвращал MockLLMClient() для любого реального ключа.
    Проверено живым вызовом на реальном ключе: Yandex говорит тем же классическим
    chat/completions протоколом, что и OpenAIClient уже реализует — новый класс не нужен."""

    client = build_llm_client(
        provider="yandex",
        api_key="k",
        model="gpt://folder-id/aliceai-llm-flash/latest",
        base_url="https://ai.api.cloud.yandex.net/v1",
    )
    assert isinstance(client, OpenAIClient)
    assert client.disable_thinking is False


def test_build_llm_client_unknown_provider_falls_back_to_mock() -> None:
    """не падает и не молчит подозрительно — просто безопасный дефолт для нераспознанного
    значения (опечатка в LLM_PROVIDER и т.п.)."""

    client = build_llm_client(provider="totally-unknown", api_key="k", model="m", base_url="https://x")
    assert isinstance(client, MockLLMClient)
