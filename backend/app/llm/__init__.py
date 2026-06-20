"""LLM provider abstraction."""

from .base import BaseLLMClient
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
            disable_thinking=normalized_provider == "openai_compatible",
        )

    return MockLLMClient()


def get_system_prompt() -> str:
    """Return the fixed system prompt."""

    return SYSTEM_PROMPT
