"""domain-aware проверки регулируемых/опасных тем."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..knowledge import normalize_text
from .constants import MEDICAL_KEYWORDS
from .extractors import contains_keyword

MEDICAL_RESTRICTED_CATEGORIES = {
    "medical",
    "medical_advice",
    "medical_treatment",
    "diagnosis",
    "treatment",
}


def _profile_value(domain_profile: Any, key: str) -> Any:
    if isinstance(domain_profile, Mapping):
        return domain_profile.get(key)
    return getattr(domain_profile, key, None)


def get_restricted_categories(domain_profile: Any) -> list[str]:
    """возвращает restricted категории, заданные профилем домена."""

    restricted_advice = _profile_value(domain_profile, "restricted_advice")
    if not restricted_advice:
        return []
    if not isinstance(restricted_advice, list):
        return []
    return [str(category).strip() for category in restricted_advice if str(category).strip()]


def _has_medical_restrictions(categories: list[str]) -> bool:
    normalized_categories = {
        str(category).strip().lower()
        for category in categories
    } | {normalize_text(category).replace(" ", "_") for category in categories}
    return bool(normalized_categories & MEDICAL_RESTRICTED_CATEGORIES)


def has_medical_restricted_category(domain_profile: Any) -> bool:
    """возвращает true, если профиль домена включает медицинские ограничения."""

    return _has_medical_restrictions(get_restricted_categories(domain_profile))


def is_restricted_question(message: str, domain_profile: Any) -> tuple[bool, str | None]:
    """
    возвращает (is_restricted, category).

    Keyword fallback применяется только для категорий, явно включённых в
    domain_profile.restricted_advice. Для generic/auto доменов медицинские
    слова не должны срабатывать сами по себе.
    """

    categories = get_restricted_categories(domain_profile)
    if not categories:
        return False, None

    normalized_message = normalize_text(message)
    if _has_medical_restrictions(categories) and contains_keyword(normalized_message, MEDICAL_KEYWORDS):
        return True, "medical"

    return False, None
