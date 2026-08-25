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


def _variant_raw_stems(service, variant: dict[str, Any]) -> set[str]:
    label = variant_label(service, variant)
    name = str(variant.get("name") or "")
    return {
        _stem(token)
        for token in _tokens(f"{label} {name}")
        if len(token) >= 3 and token not in QUERY_STOP_TOKENS
    }


def _common_variant_stems(service, variants: list[dict[str, Any]]) -> set[str]:
    """Стемы, встречающиеся у БОЛЬШИНСТВА вариантов услуги — не несут различительной силы
    между ними. Живой баг (нагрузочный тест, 2026-08-25): "MicroLaserPeel" — бренд ВНУТРИ
    имени каждого варианта Лазерного пилинга, не самой услуги, поэтому _service_stems его не
    ловил. Запрос "microlaserpeel внешняя сторона плеча" из-за этого матчил ВСЕ варианты
    (бренд пересекался с каждым), а не сужал до конкретных — без бренда та же фраза сужала
    правильно. Порог именно "больше половины", не строгое пересечение по ВСЕМ вариантам —
    у Лазерного пилинга реально 12 из 13 вариантов "MicroLaserPeel", один "NanoLaserPeel"
    (другой бренд той же услуги), так что 100%-е пересечение его вообще не находило. Тут не
    важно, откуда взялось общее слово (бренд, аппарат, что угодно повторяющееся) — если оно
    есть почти у каждого варианта, оно не помогает различить их."""

    variant_dicts = [variant for variant in variants if isinstance(variant, dict)]
    if len(variant_dicts) < 2:
        return set()
    counts: dict[str, int] = {}
    for variant in variant_dicts:
        for stem in _variant_raw_stems(service, variant):
            counts[stem] = counts.get(stem, 0) + 1
    threshold = len(variant_dicts) / 2
    return {stem for stem, count in counts.items() if count > threshold}


def _variant_stems(service, variant: dict[str, Any], *, exclude: set[str] = frozenset()) -> set[str]:
    service_stems = _service_stems(service)
    return {
        stem
        for stem in _variant_raw_stems(service, variant)
        if stem not in service_stems and stem not in exclude
    }


def find_variant_matches(service, message: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Ищет вариант только внутри выбранной услуги, без глобального fuzzy по KB."""

    variants = getattr(service, "variants", []) or []
    if not variants:
        return []

    # Точное имя варианта в сообщении важнее нечёткого совпадения по стемам: короткие
    # уточнения вроде "Т зона" после стемминга дают пустой набор слов (однобуквенный
    # токен и "зона" как общий стоп-токен отфильтровываются), поэтому их иначе не найти,
    # даже если пользователь дословно скопировал название варианта из ответа бота.
    normalized_message = normalize_text(message)
    exact_matches = [
        (variant, normalized_name)
        for variant in variants
        if isinstance(variant, dict)
        and (normalized_name := normalize_text(str(variant.get("name") or "")))
        and normalized_name in normalized_message
    ]
    if exact_matches:
        # Более длинное имя — более специфичное совпадение: короткое имя может быть
        # подстрокой более длинного ("Механическая чистка лица" входит в
        # "Механическая чистка лица ( Т зона )"), а не отдельным независимым вариантом.
        longest = max(len(name) for _, name in exact_matches)
        return [variant for variant, name in exact_matches if len(name) == longest][:limit]

    query_stems = _query_stems(message)
    if not query_stems:
        return []

    common_stems = _common_variant_stems(service, variants)
    matches: list[dict[str, Any]] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        stems = _variant_stems(service, variant, exclude=common_stems)
        if query_stems & stems:
            matches.append(variant)
        if len(matches) >= limit:
            break
    return matches
