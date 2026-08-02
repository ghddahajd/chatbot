"""вспомогательные функции для извлечения данных в политике."""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from ..knowledge import KnowledgeBase, normalize_text
from ..models import MessageRole, Session
from .constants import (
    KNOWN_CITY_FORMS,
    LOCATION_MISMATCH_KEYWORDS,
    LOCATION_PATTERNS,
    NEGATIVE_MESSAGES,
    PHONE_PATTERN,
)
from .variants import _stem, _tokens

try:
    from rapidfuzz.distance import Levenshtein
except ImportError:  # pragma: no cover - local dev can run before deps are installed.
    Levenshtein = None


def _distance_at_most_one(left: str, right: str) -> bool:
    if Levenshtein is not None:
        return Levenshtein.distance(left, right) <= 1

    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False

    edits = 0
    i = 0
    j = 0
    while i < len(left) and j < len(right):
        if left[i] == right[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(left) == len(right):
            i += 1
            j += 1
        elif len(left) > len(right):
            i += 1
        else:
            j += 1
    return True


# Некоторые фразы в keyword-списках намеренно обрезаны как стем (например "во сколько
# открывае" вместо "открываете"/"открываетесь"), чтобы ловить все словоформы через
# substring-логику. Сохраняем это через префиксное сравнение, но только для достаточно
# длинных токенов keyword'а — иначе короткие токены ("во", "не") будут случайно матчить
# префиксом совсем другие слова ("вопрос" начинается на "во").
_TOKEN_PREFIX_MIN_LENGTH = 5


def _keyword_token_matches(message_token: str, keyword_token: str) -> bool:
    if message_token == keyword_token:
        return True
    return len(keyword_token) >= _TOKEN_PREFIX_MIN_LENGTH and message_token.startswith(keyword_token)


def _contains_token_sequence(tokens: list[str], keyword_tokens: list[str]) -> bool:
    span = len(keyword_tokens)
    return any(
        all(
            _keyword_token_matches(tokens[start + offset], keyword_tokens[offset])
            for offset in range(span)
        )
        for start in range(len(tokens) - span + 1)
    )


def contains_keyword(normalized_text: str, keywords: set[str]) -> bool:
    tokens = normalized_text.split()
    token_set = set(tokens)
    for keyword in keywords:
        if " " in keyword:
            # Многословные фразы матчим по последовательности целых токенов, а не сырой
            # подстрокой по всей строке — иначе граница слов может случайно "склеить" фразу
            # (например "мне нужно" содержит "не нужно" как символьную подстроку: м[не нужно]).
            if _contains_token_sequence(tokens, keyword.split()):
                return True
            continue
        if len(keyword) <= 3:
            if keyword in token_set:
                return True
            continue
        if keyword in normalized_text:
            return True
    return False


def contains_exact_token(normalized_text: str, tokens: set[str]) -> bool:
    """Строгая проверка вхождения ЦЕЛОГО токена — в отличие от contains_keyword, не матчит
    по сырой подстроке для длинных однословных ключей. Нужна для слов, у которых есть частая
    приставочная форма с противоположным смыслом ("дорого" vs "недорого", "дешевле" vs
    "подешевле") — contains_keyword такие приставки не отличает."""

    return bool(set(normalized_text.split()) & tokens)


def fuzzy_contains(normalized_text: str, keywords: set[str], *, min_len: int = 4) -> bool:
    """Узкий fuzzy-match для UX-слов, не для safety/medical."""

    tokens = [token for token in normalized_text.split() if token]
    for keyword in keywords:
        if " " in keyword:
            if keyword in normalized_text:
                return True
            keyword_tokens = [token for token in keyword.split() if token]
            if not keyword_tokens:
                continue
            for index in range(0, len(tokens) - len(keyword_tokens) + 1):
                window = tokens[index : index + len(keyword_tokens)]
                if all(
                    len(token) >= min_len
                    and len(keyword_token) >= min_len
                    and _distance_at_most_one(token, keyword_token)
                    for token, keyword_token in zip(window, keyword_tokens)
                ):
                    return True
            continue
        if len(keyword) < min_len:
            if keyword in normalized_text:
                return True
            continue
        for token in tokens:
            if len(token) >= min_len and _distance_at_most_one(token, keyword):
                return True
    return False


def extract_phone(message: str) -> Optional[str]:
    match = PHONE_PATTERN.search(message)
    if match is None:
        return None
    phone = re.sub(r"[^\d+]", "", match.group(0))
    if phone.startswith("8"):
        phone = "+7" + phone[1:]
    return phone


def _service_field(service: Any, field_name: str) -> object:
    if isinstance(service, dict):
        return service.get(field_name)
    return getattr(service, field_name, None)


def _known_service_stems(known_services: Iterable[Any] | None) -> set[str]:
    if known_services is None:
        return set()

    stems: set[str] = set()
    for service in known_services:
        values = [
            _service_field(service, "name"),
            _service_field(service, "category"),
        ]
        synonyms = _service_field(service, "synonyms")
        if isinstance(synonyms, list):
            values.extend(synonyms)
        for value in values:
            if not isinstance(value, str):
                continue
            stems.update(
                _stem(token)
                for token in _tokens(value)
                if len(token) >= 3
            )
    return stems


# Позитивные маркеры — если сообщение грамматически указывает, ГДЕ имя, берём именно
# оттуда, а не гадаем "первое слово не из стоп-листа". Устраняет целый класс багов
# (не только конкретные слова "здравствуйте"/"это"/"меня", которые случайно не попали в
# список), а не 9 частных случаев из него. Порядок — от однозначных к более слабым сигналам.
NAME_MARKER_PATTERNS = (
    re.compile(r"\bменя\s+зовут\s+([а-яё]+)", re.IGNORECASE),
    re.compile(r"\bзовут\s+меня\s+([а-яё]+)", re.IGNORECASE),
    # обратный порядок слов — "меня Тимур зовут" — не менее естественная разговорная форма
    re.compile(r"\bменя\s+([а-яё]+)\s+зовут\b", re.IGNORECASE),
    re.compile(r"\bзовут\s+([а-яё]+)", re.IGNORECASE),
    re.compile(r"\bмо[её]\s+имя\s+([а-яё]+)", re.IGNORECASE),
    re.compile(r"\bимя\s*[-:]?\s*([а-яё]+)", re.IGNORECASE),
    re.compile(r"\bэто\s+([а-яё]+)", re.IGNORECASE),
    re.compile(r"^\s*я\s+([а-яё]+)", re.IGNORECASE),
)


def extract_name(
    message: str,
    phone: Optional[str],
    *,
    known_services: Iterable[Any] | None = None,
) -> Optional[str]:
    source = message
    if phone is not None:
        match = PHONE_PATTERN.search(message)
        if match is not None:
            line_start = message.rfind("\n", 0, match.start()) + 1
            line_end = message.find("\n", match.end())
            if line_end == -1:
                line_end = len(message)
            line = message[line_start:line_end]
            local_start = match.start() - line_start
            local_end = match.end() - line_start
            before_phone = line[:local_start].strip()
            after_phone = line[local_end:].strip()
            source = before_phone or after_phone

    cleaned = PHONE_PATTERN.sub(" ", source)
    if phone is not None:
        cleaned = cleaned.replace(phone, " ")
        cleaned = cleaned.replace(phone.removeprefix("+"), " ")
    cleaned = re.sub(r"[\d+\-\(\)]", " ", cleaned)
    cleaned = re.sub(r"[,;:.!?]", " ", cleaned)

    stop_words = {
        "хочу",
        "можно",
        "записаться",
        "записать",
        "запиши",
        "запишите",
        "заявку",
        "заявка",
        "телефон",
        "номер",
        "оставить",
        "оставляю",
        "на",
        "по",
        # приветствия/филлеры — раньше отсутствовали, из-за этого "Здравствуйте"/"Добрый"/
        # "Меня"/"Это" извлекались как имя вместо реального слова после них.
        "здравствуйте",
        "здравствуй",
        "привет",
        "приветствую",
        "добрый",
        "доброе",
        "день",
        "вечер",
        "утро",
        "это",
        "меня",
        "мое",
        "моё",
        "мой",
        "моя",
        "имя",
        "зовут",
        "я",
        "мне",
        "пожалуйста",
        "спасибо",
    }
    service_stems = _known_service_stems(known_services)

    for pattern in NAME_MARKER_PATTERNS:
        match = pattern.search(cleaned)
        if match is None:
            continue
        normalized_word = normalize_text(match.group(1))
        if len(normalized_word) < 2 or normalized_word in stop_words:
            continue
        if len(normalized_word) >= 3 and _stem(normalized_word) in service_stems:
            continue
        return normalized_word.title()

    for word in cleaned.split():
        normalized_word = normalize_text(word)
        if len(normalized_word) < 2 or normalized_word in stop_words:
            continue
        if re.search(r"[A-Za-zА-Яа-яЁё]", normalized_word):
            if len(normalized_word) >= 3 and _stem(normalized_word) in service_stems:
                return None
            return normalized_word.title()
    return None


def find_unsupported_city(normalized_message: str, company_city: str) -> Optional[str]:
    normalized_company_city = normalize_text(company_city)
    if f"не из {normalized_company_city}" in normalized_message:
        return f"not_{company_city}"

    for city_form, city_name in KNOWN_CITY_FORMS.items():
        if normalize_text(city_name) == normalized_company_city:
            continue
        # Целыми токенами, не подстрокой — иначе форма города может оказаться спрятана
        # внутри неродственного слова (например "казани" внутри "противопоказания").
        matches = (
            contains_keyword(normalized_message, {city_form})
            if " " in city_form
            else contains_exact_token(normalized_message, {city_form})
        )
        if matches:
            return city_name
    return None


def mentions_company_city(normalized_message: str, company_city: str) -> bool:
    normalized_company_city = normalize_text(company_city)
    company_city_forms = {
        city_form
        for city_form, city_name in KNOWN_CITY_FORMS.items()
        if normalize_text(city_name) == normalized_company_city
    }
    company_city_forms.add(normalized_company_city)

    if contains_keyword(normalized_message, LOCATION_MISMATCH_KEYWORDS):
        return False

    for city_form in company_city_forms:
        if f"не из {city_form}" in normalized_message or f"не в {city_form}" in normalized_message:
            return False
        if city_form in normalized_message:
            return True
    return False


def is_location_mismatch(message: str, normalized_message: str, company_city: str) -> bool:
    normalized_company_city = normalize_text(company_city)
    if mentions_company_city(normalized_message, company_city):
        return False
    if contains_keyword(normalized_message, LOCATION_MISMATCH_KEYWORDS):
        return True
    if find_unsupported_city(normalized_message, company_city):
        return True

    for pattern in LOCATION_PATTERNS:
        match = pattern.search(message)
        if match is None:
            continue
        location = normalize_text(match.group(1))
        if location and normalized_company_city not in location:
            return True
    return False


def last_service_from_history(session: Session, knowledge_base: KnowledgeBase) -> Optional[str]:
    previous_messages = session.messages[:-1]
    barrier_keywords = {
        "ботекс",
        "ботокс",
        "ботулинотерапия",
        "филлер",
        "филлеры",
        "контурная пластика",
        "увеличение губ",
        "не из",
        "онлайн",
        "удаленно",
        "удалённо",
        "дистанционно",
        "далеко",
        "покажи услуги",
        "показать услуги",
        "посмотреть услуги",
        "список услуг",
        "прайс",
        "что у вас есть",
    }
    for history_message in reversed(previous_messages[-8:]):
        if history_message.role != MessageRole.USER:
            continue
        normalized_history = normalize_text(history_message.text)
        if contains_keyword(normalized_history, barrier_keywords):
            return None
        service = knowledge_base.search_service(history_message.text)
        if service is not None:
            return service.id
    return None
