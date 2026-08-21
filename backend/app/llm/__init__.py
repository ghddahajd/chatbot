"""абстракция llm-провайдера."""

from .base import BaseLLMClient
from .classification import (
    IntentClassification,
    classification_to_legacy_result,
    normalize_intent_classification,
    safe_normalize_intent_classification,
)
from .mock import MockLLMClient
from .openai_compatible import (
    OpenAIClient,
    enforce_required_disclaimers,
    enforce_small_talk_pivot,
)
from .parsing import normalize_classification_result, tolerant_json_parse
from .prompts import (
    DEFAULT_FALLBACK,
    INTENT_CLASSIFICATION_PROMPT,
    PRICE_DISCLAIMER,
    SMALL_TALK_PROMPT,
    SYSTEM_PROMPT,
    build_system_prompt,
)


def build_llm_client(
    provider: str,
    api_key: str,
    model: str,
    base_url: str,
) -> BaseLLMClient:
    """выбирает llm-провайдера по умолчанию."""

    normalized_provider = provider.lower().strip()
    if normalized_provider == "mock" or not api_key:
        return MockLLMClient()

    if normalized_provider in {"openai", "gemini", "openai_compatible", "yandex"}:
        return OpenAIClient(
            api_key=api_key,
            model=model,
            base_url=base_url,
            # disable_thinking — костыль под chat_template_kwargs для локальных thinking-моделей
            # (Ollama/qwen), не под реальные хостед-провайдеры типа Yandex — им это не нужно и
            # может сломать запрос лишним полем.
            disable_thinking=normalized_provider == "openai_compatible",
        )

    return MockLLMClient()


def get_system_prompt() -> str:
    """возвращает фиксированный system prompt."""

    return build_system_prompt()
