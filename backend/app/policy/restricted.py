"""domain-aware проверки регулируемых/опасных тем."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..knowledge import normalize_text
from .constants import MEDICAL_KEYWORDS
from .extractors import contains_keyword, contains_keyword_lemma, lemmatize_tokens

MEDICAL_RESTRICTED_CATEGORIES = {
    "medical",
    "medical_advice",
    "medical_treatment",
    "diagnosis",
    "treatment",
}
REMOTE_SAFETY_CATEGORIES = {"remote_safety_assessment", "safety_assessment"}
REMOTE_SAFETY_KEYWORDS = {
    "можно ездить",
    "можно ли ездить",
    "безопасно ездить",
    "опасно ездить",
    "стучит",
    "скрипит",
    "горит чек",
    "загорелся чек",
    "тормоза",
    "педаль тормоза",
    "руль бьет",
    "руль бьёт",
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
    return bool(_normalized_categories(categories) & MEDICAL_RESTRICTED_CATEGORIES)


def _normalized_categories(categories: list[str]) -> set[str]:
    return {
        str(category).strip().lower()
        for category in categories
    } | {normalize_text(category).replace(" ", "_") for category in categories}


def has_medical_restricted_category(domain_profile: Any) -> bool:
    """возвращает true, если профиль домена включает медицинские ограничения."""

    return _has_medical_restrictions(get_restricted_categories(domain_profile))


_medical_single_word_lemmas: set[str] | None = None


def _medical_lemma_set() -> set[str]:
    # Живой баг (аудит §2026-08-06): интент-классификация не всегда тегнёт сообщение как
    # medical_advice/regulated_advice (у локального фолбэк-классификатора это особенно легко
    # промахнуться на многословных вопросах), и is_restricted_question — единственная
    # оставшаяся страховка. Раньше она матчила MEDICAL_KEYWORDS буквальной подстрокой ("опасно"
    # не матчило "опасен" — другая словоформа, не подстрока). Считаем леммы ключевых слов один
    # раз за процесс, не на каждое сообщение.
    global _medical_single_word_lemmas
    if _medical_single_word_lemmas is None:
        single_words = {keyword for keyword in MEDICAL_KEYWORDS if " " not in keyword}
        lemmas: set[str] = set()
        for word in single_words:
            lemmas.update(lemmatize_tokens(word))
        _medical_single_word_lemmas = lemmas
    return _medical_single_word_lemmas


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
    if _has_medical_restrictions(categories) and (
        contains_keyword(normalized_message, MEDICAL_KEYWORDS)
        or contains_keyword_lemma(normalized_message, _medical_lemma_set())
    ):
        return True, "medical"
    if _normalized_categories(categories) & REMOTE_SAFETY_CATEGORIES:
        if contains_keyword(normalized_message, REMOTE_SAFETY_KEYWORDS):
            return True, "remote_safety_assessment"

    return False, None
