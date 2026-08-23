"""domain-aware проверки регулируемых/опасных тем."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..knowledge import normalize_text
from .constants import HARD_RESTRICTED_KEYWORDS, MEDICAL_KEYWORDS
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
    #
    # С 2026-08-22 источник — объединение с HARD_RESTRICTED_KEYWORDS (аудит §2026-08-22): эта
    # функция раньше видела только MEDICAL_KEYWORDS, хотя более широкий список уже существовал
    # (использовался как rescue-gate в __init__.py, уже ПОСЛЕ того, как это решение принято).
    # Живой репро: "я беременной делать можно?" не ловилось — короткая форма "беременна" (тут)
    # и полная "беременной"/"беременным" — разные леммы у pymorphy2, а более широкий список не
    # мог спасти, потому что до него дело не доходило.
    global _medical_single_word_lemmas
    if _medical_single_word_lemmas is None:
        single_words = {
            keyword for keyword in (HARD_RESTRICTED_KEYWORDS | MEDICAL_KEYWORDS) if " " not in keyword
        }
        lemmas: set[str] = set()
        for word in single_words:
            lemmas.update(lemmatize_tokens(word))
        _medical_single_word_lemmas = lemmas
    return _medical_single_word_lemmas


# Живой баг (аудит §2026-08-22, "Ниже"): "Я уже третий раз пишу сюда и никто не отвечает
# нормально! Что за безобразие" — "нормально" в MEDICAL_KEYWORDS (нужно для легитимного "это
# нормально после процедуры?", см. test_benign_pain_question_marked_calm_not_urgent — не
# трогаем) ложно матчило чистую жалобу на сервис. Из-за этого medical_requested перехватывал
# сообщение РАНЬШЕ, чем COMPLAINT_ESCALATION_KEYWORDS вообще успевал его проверить (medical
# safety стоит строго первым приоритетом в policy/__init__.py, намеренно — не меняем порядок).
# "отвечает/отвечают/пишет/пишут/работает/работают нормально" — коллокация про качество
# ответа/сервиса, не про медицинскую норму; исключаем конкретно её, не трогая слово в остальных
# контекстах.
NON_MEDICAL_NORMALNO_COLLOCATIONS = {
    "отвечает нормально",
    "отвечают нормально",
    "отвечаете нормально",
    "пишет нормально",
    "пишут нормально",
    "пишете нормально",
    "работает нормально",
    "работают нормально",
}
_normalno_lemmas: set[str] | None = None


def _normalno_lemma_set() -> set[str]:
    global _normalno_lemmas
    if _normalno_lemmas is None:
        _normalno_lemmas = set(lemmatize_tokens("нормально"))
    return _normalno_lemmas


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
    medical_keywords = HARD_RESTRICTED_KEYWORDS | MEDICAL_KEYWORDS
    medical_lemmas = _medical_lemma_set()
    if contains_keyword(normalized_message, NON_MEDICAL_NORMALNO_COLLOCATIONS):
        medical_keywords = medical_keywords - {"нормально"}
        medical_lemmas = medical_lemmas - _normalno_lemma_set()
    if _has_medical_restrictions(categories) and (
        contains_keyword(normalized_message, medical_keywords)
        or contains_keyword_lemma(normalized_message, medical_lemmas)
    ):
        return True, "medical"
    if _normalized_categories(categories) & REMOTE_SAFETY_CATEGORIES:
        if contains_keyword(normalized_message, REMOTE_SAFETY_KEYWORDS):
            return True, "remote_safety_assessment"

    return False, None
