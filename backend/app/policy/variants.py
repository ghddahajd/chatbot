"""контекстный поиск вариантов внутри уже выбранной услуги."""

from __future__ import annotations

import re
from typing import Any

from ..knowledge import normalize_text


VARIANT_LIST_KEYWORDS = {
    "какие зоны",
    "какие есть зоны",
    "зоны",
    "зона",
    "варианты",
    "какие варианты",
    "позиции",
    "какие позиции",
    "области",
    "какие области",
}
VARIANT_LIST_PHRASE_KEYWORDS = {keyword for keyword in VARIANT_LIST_KEYWORDS if " " in keyword}
VARIANT_LIST_TOKEN_KEYWORDS = {keyword for keyword in VARIANT_LIST_KEYWORDS if " " not in keyword}

QUERY_STOP_TOKENS = {
    "а",
    "и",
    "или",
    "по",
    "про",
    "на",
    "в",
    "во",
    "для",
    "есть",
    "можно",
    "какие",
    "какая",
    "какой",
    "какое",
    "вариант",
    "варианты",
    "зона",
    "зоны",
    "позиция",
    "позиции",
}

STEM_ENDINGS = (
    "ами",
    "ями",
    "ого",
    "ему",
    "ыми",
    "ими",
    "ах",
    "ях",
    "ов",
    "ев",
    "ей",
    "ой",
    "ом",
    "ем",
    "ая",
    "яя",
    "ое",
    "ее",
    "ые",
    "ие",
    "а",
    "я",
    "ы",
    "и",
    "е",
    "у",
    "ю",
)


def is_variant_list_question(message: str) -> bool:
    normalized = normalize_text(message)
    if any(keyword in normalized for keyword in VARIANT_LIST_PHRASE_KEYWORDS):
        return True
    return bool(set(_tokens(message)) & VARIANT_LIST_TOKEN_KEYWORDS)


def variant_label(service, variant: dict[str, Any]) -> str:
    name = str(variant.get("name") or "").strip()
    if not name:
        return ""

    service_name = str(getattr(service, "name", "") or "").strip()
    if service_name and name.casefold().startswith(service_name.casefold()):
        label = name[len(service_name) :].strip(" -–—:()")
        if label:
            return label

    parts = re.split(r"\s[-–—]\s", name, maxsplit=1)
    if len(parts) == 2 and parts[1].strip():
        return parts[1].strip()
    return name


def variant_price_line(service, variant: dict[str, Any]) -> str:
    label = variant_label(service, variant)
    price_text = str(variant.get("price_text") or "").strip()
    return f"{label} — {price_text}" if price_text else label


def variant_list_labels(service, *, limit: int = 8) -> list[str]:
    labels: list[str] = []
    for variant in getattr(service, "variants", []) or []:
        if not isinstance(variant, dict):
            continue
        label = variant_label(service, variant)
        if label:
            labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def _stem(token: str) -> str:
    if len(token) <= 3:
        return token
    for ending in STEM_ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= 3:
            return token[: -len(ending)]
    return token


def _tokens(value: str) -> list[str]:
    return [token for token in normalize_text(value).split() if token]


def _service_stems(service) -> set[str]:
    values = [getattr(service, "name", ""), getattr(service, "category", "")]
    values.extend(getattr(service, "synonyms", []) or [])
    return {_stem(token) for value in values for token in _tokens(str(value))}


def _query_stems(message: str) -> set[str]:
    return {
        _stem(token)
        for token in _tokens(message)
        if len(token) >= 3 and token not in QUERY_STOP_TOKENS
    }


def _variant_stems(service, variant: dict[str, Any]) -> set[str]:
    service_stems = _service_stems(service)
    label = variant_label(service, variant)
    name = str(variant.get("name") or "")
    return {
        _stem(token)
        for token in _tokens(f"{label} {name}")
        if len(token) >= 3 and _stem(token) not in service_stems and token not in QUERY_STOP_TOKENS
    }


def find_variant_matches(service, message: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Ищет вариант только внутри выбранной услуги, без глобального fuzzy по KB."""

    variants = getattr(service, "variants", []) or []
    if not variants:
        return []

    query_stems = _query_stems(message)
    if not query_stems:
        return []

    matches: list[dict[str, Any]] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        stems = _variant_stems(service, variant)
        if query_stems & stems:
            matches.append(variant)
        if len(matches) >= limit:
            break
    return matches
