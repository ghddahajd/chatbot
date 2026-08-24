"""локальная классификация намерений для политики."""

from __future__ import annotations

from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Optional

from ..knowledge import _token_prefix_match, normalize_text
from .constants import (
    ALLOWED_CLASSIFIER_INTENTS,
    BOOKING_KEYWORDS,
    BOT_IDENTITY_SIGNAL_KEYWORDS,
    CLARIFY_SHORT_MESSAGES,
    COSMETIC_CONCERN_KEYWORDS,
    CONTACT_LINK_KEYWORDS,
    DURATION_KEYWORDS,
    FAQ_QUESTION_KEYWORDS,
    LEAD_REQUEST_KEYWORDS,
    OFF_TOPIC_KEYWORDS,
    OPERATOR_REQUEST_KEYWORDS,
    PRICE_FUZZY_EXCLUDE_TOKENS,
    PRICE_KEYWORDS,
    PROMPT_INJECTION_KEYWORDS,
    SERVICE_LIST_FAST_MESSAGES,
    SERVICE_LIST_KEYWORDS,
    SMALL_TALK_FUZZY_EXCLUDE,
    SMALL_TALK_KEYWORDS,
    UNKNOWN_SERVICE_KEYWORDS,
    VISIT_KEYWORDS,
)
from .extractors import (
    _distance_at_most_one,
    contains_exact_token,
    contains_keyword,
    fuzzy_contains,
    is_location_mismatch,
    lemmatize_known_name,
    mentions_company_city,
)
from .restricted import is_restricted_question


# "чем X отличается от Y" / "в чем разница между X и Y" — естественный порядок слов вставляет
# название услуги/аппарата МЕЖДУ "чем" и "отличается" ("чем Morpheus8 отличается от MiniFX"),
# так что contains_keyword (сравнивает смежные токены) фразу "чем отличается" не ловит. Токены
# по отдельности, в любом порядке в сообщении, — надёжнее для этого паттерна сравнения.
COMPARISON_QUESTION_TOKENS = {"чем", "отличается", "отличие", "различие", "разница"}


def _looks_like_comparison_question(normalized_message: str) -> bool:
    tokens = set(normalized_message.split())
    has_subject = bool(tokens & {"чем", "разница", "отличие", "различие"})
    has_predicate = bool(tokens & {"отличается", "отличаются", "разница", "отличие", "различие"})
    return has_subject and has_predicate


def _known_service_terms(service_payload: dict[str, Any]) -> list[str]:
    terms = [str(service_payload.get("name") or "")]
    synonyms = service_payload.get("synonyms") or []
    if isinstance(synonyms, list):
        terms.extend(str(synonym) for synonym in synonyms)
    return [term for term in terms if term]


def _tenant_catalog_text(known_services: list[dict[str, Any]]) -> str:
    # Категория намеренно НЕ идёт через _known_service_terms — та же функция используется для
    # разрешения service_id по сообщению, и категория (часто общая у нескольких услуг) как
    # matchable term там путает, к какой именно услуге относится сообщение. Здесь нужен только
    # текст для проверки "это слово вообще про нашу тему", отдельный список безопаснее.
    terms: list[str] = []
    for service_payload in known_services:
        terms.extend(_known_service_terms(service_payload))
        category = service_payload.get("category")
        if category:
            terms.append(str(category))
    return normalize_text(" ".join(terms))


@lru_cache(maxsize=256)
def _lemma_or_self(word: str) -> str:
    return lemmatize_known_name(word) or word


def _exclude_keywords_covered_by_catalog(keywords: set[str], catalog_text: str) -> set[str]:
    """OFF_TOPIC_KEYWORDS/UNKNOWN_SERVICE_KEYWORDS общие для всех тенантов и содержат лексику
    конкретных доменов ("уборка", "авто", "филлеры") — живой баг: для клининговой компании
    "уборка" в её же каталоге услуг всё равно матчилась как "такой услуги нет". Убираем из
    списка любое ключевое слово, которое и так встречается в name/synonyms/category самого
    тенанта — оно не может быть "не нашей темой", если это буквально наша услуга.

    Сравнение идёт и дословно, и по лемме — сама услуга в каталоге почти всегда в именительном
    падеже ("Уборка квартир"), а в списке ключевых слов рядом лежат разные падежные формы
    ("уборка"/"уборку"/"уборке") как отдельные строки; дословное сравнение ловит только
    случайно совпавшую форму, лемма ловит все."""

    if not catalog_text:
        return keywords
    catalog_lemma_text = _lemma_or_self(catalog_text)
    remaining = set()
    for keyword in keywords:
        if contains_keyword(catalog_text, {keyword}):
            continue
        if contains_keyword(catalog_lemma_text, {_lemma_or_self(keyword)}):
            continue
        remaining.add(keyword)
    return remaining


def _service_token_match(term_token: str, query_token: str, *, allow_fuzzy: bool = True) -> bool:
    if _token_prefix_match(term_token, query_token):
        return True
    if not allow_fuzzy:
        return False
    if len(term_token) < 6 or len(query_token) < 6:
        return (
            len(term_token) >= 5
            and len(query_token) >= 5
            and term_token[0] == query_token[0]
            and _distance_at_most_one(term_token, query_token)
        )
    first_letters = {term_token[0], query_token[0]}
    if term_token[0] != query_token[0] and first_letters != {"е", "э"}:
        return False
    return SequenceMatcher(None, term_token, query_token).ratio() >= 0.74


def _local_service_id(
    message: str,
    known_services: list[dict[str, Any]],
    *,
    allow_fuzzy: bool = True,
) -> Optional[str]:
    query_tokens = [token for token in normalize_text(message).split() if token]
    if not query_tokens:
        return None

    normalized_message = " ".join(query_tokens)
    if "почист" in normalized_message and "лиц" in normalized_message:
        for service_payload in known_services:
            if service_payload.get("id") == "facial_cleansing":
                return "facial_cleansing"

    # Живой баг (BICOM): "Биорезонансная терапия на аппарате BICOM" и "Диагностика на
    # аппарате BICOM BODY CHECK" делят короткий синоним "bicom"/"биком" — и у первой в прайсе
    # есть вариант "Диагностика на аппарате BICOM (до 8 лет)", ещё больше пересекающийся по
    # словам со второй услугой целиком. Цикл ниже матчит по ПОРЯДКУ услуг в списке, а не по
    # специфичности совпадения — короткий синоним у раньше идущей услуги побеждал, даже когда
    # сообщение буквально содержит точное название ДРУГОЙ услуги или её варианта целиком.
    # Отдельным проходом сначала ищем самое ДЛИННОЕ точное совпадение (name услуги или name
    # одного из её variants — оба сильные, самодостаточные сигналы) по ВСЕМ услугам; длина —
    # тот же принцип специфичности, что уже применяется в find_variant_matches (variants.py)
    # для выбора между вариантами ВНУТРИ услуги. Если ничего не нашлось — откатываемся к
    # обычному циклу name+synonyms по порядку списка, как раньше.
    best_match: tuple[int, str] | None = None
    for service_payload in known_services:
        normalized_name = normalize_text(str(service_payload.get("name") or ""))
        if normalized_name and normalized_name in normalized_message:
            if best_match is None or len(normalized_name) > best_match[0]:
                best_match = (len(normalized_name), str(service_payload.get("id")))
        for variant in service_payload.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            normalized_variant_name = normalize_text(str(variant.get("name") or ""))
            if normalized_variant_name and normalized_variant_name in normalized_message:
                if best_match is None or len(normalized_variant_name) > best_match[0]:
                    best_match = (len(normalized_variant_name), str(service_payload.get("id")))
    if best_match is not None:
        return best_match[1]

    for service_payload in known_services:
        for term in _known_service_terms(service_payload):
            normalized_term = normalize_text(term)
            if normalized_term and normalized_term in normalized_message:
                return str(service_payload.get("id"))

            term_tokens = [token for token in normalized_term.split() if token]
            if term_tokens and all(
                any(
                    _service_token_match(term_token, query_token, allow_fuzzy=allow_fuzzy)
                    for query_token in query_tokens
                )
                for term_token in term_tokens
            ):
                return str(service_payload.get("id"))

    return None


def normalize_classification(raw_result: dict[str, Any]) -> dict[str, object]:
    intent = str(raw_result.get("intent") or "service_mention").strip().lower()
    if intent not in ALLOWED_CLASSIFIER_INTENTS:
        intent = "service_mention"

    service_id = raw_result.get("service_id")
    if service_id is not None:
        service_id = str(service_id).strip()
    if not service_id or service_id == "null":
        service_id = None

    try:
        confidence = float(raw_result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    normalized = {
        "intent": intent,
        "service_id": service_id,
        "confidence": min(max(confidence, 0.0), 1.0),
    }
    context_topic = str(raw_result.get("context_topic") or "").strip()
    if context_topic:
        normalized["context_topic"] = context_topic
    context_variant = raw_result.get("context_variant")
    if isinstance(context_variant, dict):
        normalized["context_variant"] = context_variant
    context_candidate_service_ids = raw_result.get("context_candidate_service_ids")
    if isinstance(context_candidate_service_ids, list) and context_candidate_service_ids:
        normalized["context_candidate_service_ids"] = context_candidate_service_ids
    return normalized


def classify_and_extract(
    message: str,
    known_services: list[dict[str, Any]],
    company_city: str = "Москва",
    domain_profile: Any | None = None,
) -> dict[str, object]:
    """локальный резервный путь, если внешний классификатор недоступен."""

    normalized_message = normalize_text(message)
    if normalized_message in CLARIFY_SHORT_MESSAGES:
        return {"intent": "clarify", "service_id": None, "confidence": 0.7}
    catalog_text = _tenant_catalog_text(known_services)
    if is_location_mismatch(message, normalized_message, company_city, known_service_text=catalog_text):
        return {"intent": "location_mismatch", "service_id": None, "confidence": 0.86}
    off_topic_keywords = _exclude_keywords_covered_by_catalog(OFF_TOPIC_KEYWORDS, catalog_text)
    unknown_service_keywords = _exclude_keywords_covered_by_catalog(UNKNOWN_SERVICE_KEYWORDS, catalog_text)

    has_off_topic_keyword = contains_keyword(normalized_message, off_topic_keywords)
    service_id = _local_service_id(message, known_services, allow_fuzzy=not has_off_topic_keyword)
    if has_off_topic_keyword and service_id is None:
        return {"intent": "off_topic", "service_id": None, "confidence": 0.82}
    if mentions_company_city(normalized_message, company_city):
        return {"intent": "clarify", "service_id": None, "confidence": 0.78}
    if contains_keyword(normalized_message, PROMPT_INJECTION_KEYWORDS):
        return {"intent": "off_topic", "service_id": None, "confidence": 0.96}
    if contains_keyword(normalized_message, OPERATOR_REQUEST_KEYWORDS) and not contains_keyword(
        normalized_message, BOT_IDENTITY_SIGNAL_KEYWORDS
    ):
        return {"intent": "operator_request", "service_id": None, "confidence": 0.9}

    if (
        (contains_keyword(normalized_message, FAQ_QUESTION_KEYWORDS) or _looks_like_comparison_question(normalized_message))
        and not fuzzy_contains(normalized_message, PRICE_KEYWORDS, exclude_tokens=PRICE_FUZZY_EXCLUDE_TOKENS)
        and not contains_keyword(normalized_message, DURATION_KEYWORDS)
    ):
        return {"intent": "faq_question", "service_id": service_id, "confidence": 0.84}
    if contains_keyword(normalized_message, unknown_service_keywords) and service_id is None:
        return {"intent": "unknown_service", "service_id": None, "confidence": 0.84}
    if service_id is None:
        service_id = _local_service_id(message, known_services)
    if normalized_message in SERVICE_LIST_FAST_MESSAGES or contains_keyword(
        normalized_message, SERVICE_LIST_KEYWORDS
    ):
        return {"intent": "list_services", "service_id": service_id, "confidence": 0.9}
    if contains_keyword(normalized_message, CONTACT_LINK_KEYWORDS) or contains_keyword(
        normalized_message, VISIT_KEYWORDS
    ):
        return {"intent": "contact_link", "service_id": None, "confidence": 0.88}
    if contains_keyword(normalized_message, LEAD_REQUEST_KEYWORDS):
        return {"intent": "lead_request", "service_id": service_id, "confidence": 0.88}
    if fuzzy_contains(normalized_message, PRICE_KEYWORDS, exclude_tokens=PRICE_FUZZY_EXCLUDE_TOKENS):
        return {"intent": "price_question", "service_id": service_id, "confidence": 0.86}
    if contains_keyword(normalized_message, BOOKING_KEYWORDS):
        return {"intent": "booking_request", "service_id": service_id, "confidence": 0.88}
    if (
        service_id is None
        and "делаете" in normalized_message.split()
        and not normalized_message.startswith(("что ", "чем "))
    ):
        return {"intent": "unknown_service", "service_id": None, "confidence": 0.82}
    is_restricted, _category = is_restricted_question(message, domain_profile)
    if is_restricted:
        return {"intent": "medical_advice", "service_id": service_id, "confidence": 0.86}
    if contains_keyword(normalized_message, COSMETIC_CONCERN_KEYWORDS):
        return {"intent": "cosmetic_concern", "service_id": service_id, "confidence": 0.82}
    if service_id:
        return {"intent": "service_mention", "service_id": service_id, "confidence": 0.78}
    if (
        len(normalized_message.split()) <= 2
        and normalized_message not in SMALL_TALK_FUZZY_EXCLUDE
        and fuzzy_contains(normalized_message, SMALL_TALK_KEYWORDS)
    ):
        return {"intent": "small_talk", "service_id": None, "confidence": 0.76}
    # Живой баг (длинный диалог, демо-тестирование, 2026-08-24): "спасибо большое, вы очень
    # помогли" (5 слов) — благодарность/прощание, но word-count-лимит выше (<=2 слова) не даёт
    # её распознать как small_talk, а реальный LLM в конце длинного диалога классифицировал
    # это как off_topic ("это не по моей части") — жёстко звучит в ответ на "спасибо". Раз мы
    # дошли ДО СЮДА, все остальные более содержательные ветки (цена/запись/услуга/медицина/
    # список услуг и т.д.) уже проверены и не совпали — значит бонус-риск, что "спасибо"
    # прячет внутри забытый настоящий вопрос, здесь уже отсутствует. contains_keyword — без
    # ограничения на длину сообщения, в отличие от fuzzy-варианта выше.
    if contains_keyword(normalized_message, {"спасибо", "благодарю"}):
        return {"intent": "small_talk", "service_id": None, "confidence": 0.8}
    return {"intent": "service_mention", "service_id": None, "confidence": 0.0}
