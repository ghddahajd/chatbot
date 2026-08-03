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

# Natasha (github.com/natasha) — офлайн NER для русского, настоящее распознавание сущностей
# вместо угадывания по регуляркам.
#
# Имена людей (PER): "меня зовут Мария Петровна" -> имя+отчество без ручных костылей на
# заглавную букву, "моя фамилия Петина" -> "Петина", не слово "фамилия"; и главное —
# "дура тупая" НЕ считает именем (regex не отличит оскорбление от имени, NER — отличает).
# Единственный найденный пробел — голое "Иван +7999..." без рамки-глагола natasha не ловит,
# поэтому ниже он остаётся как regex-фолбэк, не убран целиком.
#
# Города (LOC): раньше KNOWN_CITY_FORMS требовал вручную перечислять КАЖДОЕ падежное
# склонение города ("москва"/"москве"/"москвы") — для большинства городов в списке была только
# именительная форма, "живу в Екатеринбурге" не ловилось вообще. NER находит LOC-сущность в
# любом падеже, а отдельный морфологический тэггер лемматизирует до именительного падежа
# ("Нижнем Новгороде" -> "нижний новгород") — ловит ЛЮБОЙ город, не только перечисленные вручную.
#
# Модели грузятся один раз лениво (~0.3с на NER, +43МБ на морфологию поверх — та же embedding
# переиспользуется) — не на каждый вызов и не если функция вообще не понадобится в этом
# процессе. Если natasha не установлена или не грузится — тихо остаёмся на regex, не роняем
# сервис. NER работает на СЫРОМ (не normalize_text) тексте — регистр важен для распознавания,
# как и для имён.
_natasha_segmenter: Any = None
_natasha_embedding: Any = None
_natasha_ner_tagger: Any = None
_natasha_morph_tagger: Any = None
_natasha_morph_vocab: Any = None
_natasha_ner_load_failed = False
_natasha_morph_load_failed = False


def _load_natasha_ner() -> bool:
    global _natasha_segmenter, _natasha_embedding, _natasha_ner_tagger, _natasha_ner_load_failed
    if _natasha_ner_tagger is not None:
        return True
    if _natasha_ner_load_failed:
        return False
    try:
        from natasha import NewsEmbedding, NewsNERTagger, Segmenter

        _natasha_segmenter = Segmenter()
        _natasha_embedding = NewsEmbedding()
        _natasha_ner_tagger = NewsNERTagger(_natasha_embedding)
        return True
    except Exception:
        _natasha_ner_load_failed = True
        return False


def _load_natasha_morph() -> bool:
    global _natasha_morph_tagger, _natasha_morph_vocab, _natasha_morph_load_failed
    if _natasha_morph_tagger is not None:
        return True
    if _natasha_morph_load_failed:
        return False
    if not _load_natasha_ner():  # переиспользуем ту же embedding/segmenter, не грузим заново
        return False
    try:
        from natasha import MorphVocab, NewsMorphTagger

        _natasha_morph_vocab = MorphVocab()
        _natasha_morph_tagger = NewsMorphTagger(_natasha_embedding)
        return True
    except Exception:
        _natasha_morph_load_failed = True
        return False


def _run_ner_for_person(text: str) -> Optional[str]:
    if not text.strip() or not _load_natasha_ner():
        return None
    from natasha import PER, Doc

    doc = Doc(text)
    doc.segment(_natasha_segmenter)
    doc.tag_ner(_natasha_ner_tagger)
    for span in doc.spans:
        if span.type != PER:
            continue
        candidate = span.text.strip()
        if candidate:
            return candidate.title()
    return None


def _extract_name_via_ner(message: str) -> Optional[str]:
    return _run_ner_for_person(message)


# NER тегирует LOC любую географическую сущность — город, район, станцию метро, улицу —
# одинаково, без различия уровня. Живой баг (найден внешним аудитом, подтверждён): "я с
# Таганки"/"живу на Соколе" (районы Москвы) ловились как "не тот город" для московской
# клиники. Предлог перед сущностью — надёжный грамматический сигнал уровня в русском: "в"/"из"
# обычно вводят город ("живу В Москве", "я ИЗ Казани"), "на"/"с" — район/станцию/ориентир
# ("живу НА Соколе", "я С Таганки") тем же прилагательным падежом. Не идеально (есть города
# "на" в естественной речи не встречающиеся так), но проверено на всех живых кейсах аудита —
# отсекает все 5 ложных срабатываний по районам, не трогает ни один настоящий город.
_CITY_LEVEL_PREPOSITIONS = {"в", "из"}


def _run_ner_for_city_raw(text: str) -> Optional[str]:
    """Находит LOC-сущность НА УРОВНЕ ГОРОДА (см. _CITY_LEVEL_PREPOSITIONS) в СЫРОМ тексте,
    возвращает лемму в именительном падеже без какой-либо фильтрации по совпадению с городом
    клиники — используется и когда нужно найти "не тот город" (find_unsupported_city), и когда
    нужно подтвердить "да, тот самый" (mentions_company_city)."""

    if not text.strip() or not _load_natasha_morph():
        return None
    from natasha import LOC, Doc

    doc = Doc(text)
    doc.segment(_natasha_segmenter)
    doc.tag_morph(_natasha_morph_tagger)
    doc.tag_ner(_natasha_ner_tagger)
    for span in doc.spans:
        if span.type != LOC:
            continue
        preceding_tokens = [token for token in doc.tokens if token.stop <= span.start]
        preceding_word = preceding_tokens[-1].text.lower() if preceding_tokens else ""
        if preceding_word not in _CITY_LEVEL_PREPOSITIONS:
            continue
        span_tokens = [token for token in doc.tokens if token.start >= span.start and token.stop <= span.stop]
        for token in span_tokens:
            token.lemmatize(_natasha_morph_vocab)
        lemma = " ".join(token.lemma for token in span_tokens if token.lemma).strip()
        if lemma:
            return lemma
    return None


def _run_ner_for_city(text: str, *, skip_normalized: str) -> Optional[str]:
    """Как _run_ner_for_city_raw, но пропускает совпадение с городом клиники — не ложно
    предупреждать про "не тот город", когда LOC-сущность — это как раз родной город клиники."""

    lemma = _run_ner_for_city_raw(text)
    if lemma and lemma != skip_normalized:
        return lemma
    return None


def extract_person_lemma_via_ner(text: str) -> Optional[str]:
    """Находит PER-сущность в СЫРОМ тексте (регистр важен для распознавания) и возвращает её
    лемму (именительный падеж, нижний регистр) — для сравнения с базой (например, врачей), не
    для показа пользователю. Используется вместе с lemmatize_known_name() ниже — ОБЕ стороны
    сравнения должны идти через одну и ту же лемматизацию, иначе не совпадут: pymorphy2 не
    всегда согласован сам с собой между падежами одной и той же фамилии (проверено живьём:
    "Джалилов" (именительный) -> лемма "джалил", но "Джалилову" (дательный) -> лемма
    "джалилов" — РАЗНЫЕ строки при одинаковом человеке). Сравнивать леммы нужно по префиксу
    токена (см. _doctor_matches), не на точное равенство."""

    if not text.strip() or not _load_natasha_morph():
        return None
    from natasha import PER, Doc

    doc = Doc(text)
    doc.segment(_natasha_segmenter)
    doc.tag_morph(_natasha_morph_tagger)
    doc.tag_ner(_natasha_ner_tagger)
    for span in doc.spans:
        if span.type != PER:
            continue
        span_tokens = [token for token in doc.tokens if token.start >= span.start and token.stop <= span.stop]
        for token in span_tokens:
            token.lemmatize(_natasha_morph_vocab)
        lemma = " ".join(token.lemma for token in span_tokens if token.lemma).strip()
        if lemma:
            return lemma
    return None


def lemmatize_known_name(name: str) -> Optional[str]:
    """Лемматизирует УЖЕ ИЗВЕСТНОЕ имя (например, врача из базы клиента) — не ищет
    PER-сущность, сразу лемматизирует все токены (имя гарантированно уже нужного типа)."""

    if not name.strip() or not _load_natasha_morph():
        return None
    from natasha import Doc

    doc = Doc(name)
    doc.segment(_natasha_segmenter)
    doc.tag_morph(_natasha_morph_tagger)
    for token in doc.tokens:
        token.lemmatize(_natasha_morph_vocab)
    lemma = " ".join(token.lemma for token in doc.tokens if token.lemma).strip()
    return lemma or None


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


# Местоимения-анафоры ("а сколько ЭТО стоит?") ломают consecutive-token матчинг фраз вроде
# "сколько стоит" — слово вклинивается между частями фразы. Тот же класс проблемы, что уже
# чинили для "чем X отличается от Y" (см. _looks_like_comparison_question в intent.py) —
# только здесь дешевле убрать местоимение перед проверкой, чем городить отдельный parser.
ANAPHORIC_PRONOUNS = {"это", "он", "она", "оно", "её", "его", "их"}


def strip_anaphoric_pronouns(normalized_text: str) -> str:
    tokens = [token for token in normalized_text.split() if token not in ANAPHORIC_PRONOUNS]
    return " ".join(tokens)


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
# Второе слово опционально — для отчества/фамилии ("меня зовут Мария Петровна"). Само по себе
# в паттерне не различить, имя это или продолжение фразы ("Мария очень приятно") — IGNORECASE
# на весь паттерн не даёт положиться на регистр в самом regex. Решение — в коде ниже: второе
# слово принимаем, только если оно написано с заглавной буквы в ОРИГИНАЛЕ сообщения.
NAME_MARKER_PATTERNS = (
    re.compile(r"\bменя\s+зовут\s+([а-яё]+)(?:\s+([а-яё]+))?", re.IGNORECASE),
    re.compile(r"\bзовут\s+меня\s+([а-яё]+)(?:\s+([а-яё]+))?", re.IGNORECASE),
    # обратный порядок слов — "меня Тимур зовут" — не менее естественная разговорная форма
    re.compile(r"\bменя\s+([а-яё]+)(?:\s+([а-яё]+))?\s+зовут\b", re.IGNORECASE),
    re.compile(r"\bзовут\s+([а-яё]+)(?:\s+([а-яё]+))?", re.IGNORECASE),
    re.compile(r"\bмо[её]\s+имя\s+([а-яё]+)(?:\s+([а-яё]+))?", re.IGNORECASE),
    re.compile(r"\bимя\s*[-:]?\s*([а-яё]+)(?:\s+([а-яё]+))?", re.IGNORECASE),
    re.compile(r"\bэто\s+([а-яё]+)(?:\s+([а-яё]+))?", re.IGNORECASE),
    re.compile(r"^\s*я\s+([а-яё]+)(?:\s+([а-яё]+))?", re.IGNORECASE),
)


def extract_name(
    message: str,
    phone: Optional[str],
    *,
    known_services: Iterable[Any] | None = None,
) -> Optional[str]:
    service_stems = _known_service_stems(known_services)

    ner_candidate = _extract_name_via_ner(message)
    if ner_candidate is not None:
        # На всякий случай — та же защита, что и у regex-пути: не спутать имя услуги с именем
        # человека (по каждому слову отдельно — "Мария Петровна" может быть двумя токенами).
        # NER под это в принципе не должен попадать (это не имя собственное), но дёшево
        # подстраховаться раз уж проверка уже есть.
        words = normalize_text(ner_candidate).split()
        is_service_name = any(len(word) >= 3 and _stem(word) in service_stems for word in words)
        if not is_service_name:
            return ner_candidate

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
        # "моя фамилия Петина" — без стоп-слова fallback хватал само слово "фамилия" раньше
        # реального значения после него (тот же класс бага, что и с "имя вообще").
        "фамилия",
        "фамилию",
        "фамилии",
    }

    def _is_usable_second_word(raw_word: str) -> bool:
        # Заглавная буква в ОРИГИНАЛЕ — сигнал "это ещё одно имя собственное" (отчество/
        # фамилия), а не случайное слово продолжения фразы ("Мария очень приятно" — "очень"
        # с маленькой). Не железобетонно, но дешёво и без риска для обычного случая.
        if not raw_word or not raw_word[0].isupper():
            return False
        normalized = normalize_text(raw_word)
        if len(normalized) < 2 or normalized in stop_words:
            return False
        if len(normalized) >= 3 and _stem(normalized) in service_stems:
            return False
        return True

    for pattern in NAME_MARKER_PATTERNS:
        match = pattern.search(cleaned)
        if match is None:
            continue
        normalized_word = normalize_text(match.group(1))
        if len(normalized_word) < 2 or normalized_word in stop_words:
            continue
        if len(normalized_word) >= 3 and _stem(normalized_word) in service_stems:
            continue
        full_name = normalized_word.title()
        second_word = match.group(2) if pattern.groups >= 2 else None
        if second_word and _is_usable_second_word(second_word):
            full_name = f"{full_name} {normalize_text(second_word).title()}"
        return full_name

    # Без явного маркера ("меня зовут"/"я"/"это" и т.д.) верим только КОРОТКОМУ ответу — это
    # форма "бот спросил как зовут, человек ответил голым словом", не полноценная фраза. Считаем
    # длину БЕЗ стоп-слов — "привет, я Виктория" по сути короткий ответ (одно значимое слово),
    # хоть и состоит из 3 токенов, приветствие/местоимение не в счёт.
    #
    # NER выше уже посмотрел на СЫРОЕ сообщение и не нашёл имя — часто из-за регистра (Наташа
    # плохо ловит имена с маленькой буквы: "меня зовут мария" не находит вообще). Пробуем ЕЩЁ
    # РАЗ — не на всём сообщении, а на изолированной короткой фразе-кандидате, поднятой в
    # регистр. Вне контекста остального предложения NER даёт куда более надёжный результат:
    # проверено живьём — "леха"/"мария" находит даже с маленькой буквы после изоляции+регистра,
    # а "дура"/"дура тупая" всё равно корректно отвергает ДАЖЕ с большой буквы. Это не то же
    # самое, что просто "верить заглавной букве" (старый компромисс) — NER продолжает отличать
    # реальное имя от случайного слова семантически, не по одному только регистру. Если и это
    # ничего не даёт — сдаёмся, не угадываем вслепую первым словом (как было раньше).
    meaningful_words = [word for word in cleaned.split() if normalize_text(word) not in stop_words]
    if meaningful_words and len(meaningful_words) <= 2:
        isolated_candidate = " ".join(word.title() for word in meaningful_words)
        retry_candidate = _run_ner_for_person(isolated_candidate)
        if retry_candidate is not None:
            words = normalize_text(retry_candidate).split()
            if not any(len(word) >= 3 and _stem(word) in service_stems for word in words):
                return retry_candidate
    return None


def find_unsupported_city(
    normalized_message: str,
    company_city: str,
    *,
    message: str = "",
) -> Optional[str]:
    normalized_company_city = normalize_text(company_city)
    if f"не из {normalized_company_city}" in normalized_message:
        return f"not_{company_city}"

    # NER на СЫРОМ сообщении (регистр важен) — ловит любой город в любом падеже, не только
    # перечисленные вручную в KNOWN_CITY_FORMS. message — опциональный параметр (не все вызовы
    # его передают), поэтому это дополнение, не замена словарного пути ниже.
    if message:
        ner_city = _run_ner_for_city(message, skip_normalized=normalized_company_city)
        if ner_city:
            return " ".join(word.capitalize() for word in ner_city.split())

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


def mentions_company_city(normalized_message: str, company_city: str, *, message: str = "") -> bool:
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

    # KNOWN_CITY_FORMS вручную заполнен формами только для нескольких городов (Москва, Казань,
    # Питер) — для остальных (реальный живой случай: клиника в Ярославле) "я из Ярославля" не
    # находило ничего вообще, is_location_mismatch затем ложно решал, что клиент не из своего
    # же города. NER + лемматизация ловит любой город, не только вписанные вручную.
    if message:
        company_lemma = lemmatize_known_name(company_city) or normalized_company_city
        message_lemma = _run_ner_for_city_raw(message)
        if message_lemma and message_lemma == company_lemma:
            return True

    return False


def is_location_mismatch(message: str, normalized_message: str, company_city: str) -> bool:
    normalized_company_city = normalize_text(company_city)
    if mentions_company_city(normalized_message, company_city, message=message):
        return False
    if contains_keyword(normalized_message, LOCATION_MISMATCH_KEYWORDS):
        return True
    if find_unsupported_city(normalized_message, company_city, message=message):
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
