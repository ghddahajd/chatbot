"""валидация ответов и детерминированные резервные ответы."""

from __future__ import annotations

import logging
import re
from typing import Any

from .policy.restricted import has_medical_restricted_category


logger = logging.getLogger(__name__)
_intercept_count = 0

RAW_CONTEXT_PATTERNS = (
    re.compile(r"\bservice_id\b", re.IGNORECASE),
    re.compile(r"\bprice_text\b", re.IGNORECASE),
    re.compile(r"\bsafe_context\b", re.IGNORECASE),
    re.compile(r"\bshort_description\b", re.IGNORECASE),
    re.compile(r"\bquestion_type\b", re.IGNORECASE),
    re.compile(r"готовый безопасный смысл ответа", re.IGNORECASE),
    re.compile(r"на основе предоставленного контекста", re.IGNORECASE),
    re.compile(r"предоставленн(?:ого|ом)\s+контекст", re.IGNORECASE),
    re.compile(r"\{[^{}]*(?:service|price|company|service_id|price_text)[^{}]*\}", re.IGNORECASE | re.DOTALL),
)
UNSUPPORTED_DETAIL_PATTERNS = (
    re.compile(
        r"(?:эффективн|подойд[её]т|подходящ|подобрать|подбер[её]т|"
        r"лучше всего|для вашей кожи|част[ьи] лица|лоб|вокруг глаз|"
        r"составляющ|методик|зон[ауые] лица)",
        re.IGNORECASE,
    ),
)
UNSUPPORTED_EQUIPMENT_PATTERNS = (
    re.compile(
        r"(?:nd[\s:-]?yag|alexandrite|александритов|диодны[йе]|рубиновы[йе]|"
        r"ipl-?лазер|неодимов|эрбиев)",
        re.IGNORECASE,
    ),
)
CONSULTATION_FORBIDDEN_PATTERNS = (
    *RAW_CONTEXT_PATTERNS,
    re.compile(r"\d"),
    re.compile(r"(?:₽|руб|рублей)", re.IGNORECASE),
    *UNSUPPORTED_DETAIL_PATTERNS,
)
MEDICAL_CONSULTATION_FORBIDDEN_PATTERNS = (
    re.compile(
        r"(?:диагноз|лечени|лечить|препарат|таблет|мазь|антибиотик|назнач|"
        r"гарант|безопасн|побочн|противопоказ|симптом|аллерг|беремен|"
        r"кров|родин|осложнен|осложнён|нормально|опасн|покраснен|от[её]к)",
        re.IGNORECASE,
    ),
)


def _digit_groups(value: str) -> set[str]:
    return {re.sub(r"\D+", "", group) for group in re.findall(r"\d[\d\s]*", value)}


def _digits(value: str) -> str:
    return re.sub(r"\D+", "", value)


def _significant_words(value: str) -> set[str]:
    normalized = re.sub(r"[^a-zа-я0-9\s]+", " ", value.lower().replace("ё", "е"))
    stop_words = {"услуга", "услуги", "центр", "специалист", "процедура", "процедуры"}
    return {word for word in normalized.split() if len(word) >= 5 and word not in stop_words}


def _validate_fact_constraints(answer: str, context: dict[str, Any]) -> bool:
    question_type = context.get("question_type")
    service = context.get("service") if isinstance(context.get("service"), dict) else {}
    price = context.get("price") if isinstance(context.get("price"), dict) else {}
    phrasebook = context.get("phrasebook") if isinstance(context.get("phrasebook"), dict) else {}
    price_disclaimer = str(
        phrasebook.get("price_disclaimer")
        or "Предварительно так, точнее сообщит специалист."
    )

    if question_type == "price":
        price_text = str(price.get("price_text") or "")
        price_digits = _digits(price_text)
        answer_digits = _digits(answer)
        if price_digits and price_digits not in answer_digits:
            return False
        if _digit_groups(answer) - _digit_groups(price_text):
            return False

    if question_type == "duration":
        duration = str(service.get("duration") or "")
        if duration:
            duration_digits = _digits(duration)
            if duration_digits and duration_digits not in _digits(answer):
                return False
        elif re.search(r"\d|₽|руб", answer, re.IGNORECASE):
            return False

    if question_type == "explanation":
        if re.search(r"\d|₽|руб", answer, re.IGNORECASE):
            return False
        if any(pattern.search(answer) for pattern in UNSUPPORTED_DETAIL_PATTERNS):
            return False
        safe_words = _significant_words(
            " ".join(
                [
                    str(service.get("name") or ""),
                    str(service.get("short_description") or ""),
                ]
            )
        )
        if safe_words and not (_significant_words(answer) & safe_words):
            return False

    if question_type == "faq_question":
        if re.search(r"₽|руб(?:л|\.)", answer, re.IGNORECASE):
            return False
        article_context = context.get("article_context")
        snippets_text = ""
        if isinstance(article_context, list):
            snippets_text = " ".join(
                f"{item.get('title', '')} {item.get('snippet', '')}"
                for item in article_context
                if isinstance(item, dict)
            )
        safe_words = _significant_words(snippets_text)
        if safe_words and not (_significant_words(answer) & safe_words):
            return False

    return True


def validate_response(answer: str, context: dict[str, Any] | None = None) -> bool:
    """возвращает false, если модель вывела сырой контекст или json-подобные данные."""

    if not answer.strip():
        return False
    if any(pattern.search(answer) for pattern in RAW_CONTEXT_PATTERNS):
        return False
    if context is not None and not _validate_fact_constraints(answer, context):
        return False
    return True


def _context_has_medical_restrictions(context: dict[str, Any] | None) -> bool:
    if context is None:
        return True
    domain_profile = context.get("domain_profile") if isinstance(context.get("domain_profile"), dict) else {}
    return has_medical_restricted_category(domain_profile)


def validate_consultation_response(answer: str, context: dict[str, Any] | None = None) -> bool:
    """более мягкая защита для консультационных ответов только от llm."""

    if not answer.strip():
        return False
    if any(pattern.search(answer) for pattern in CONSULTATION_FORBIDDEN_PATTERNS):
        return False
    if any(pattern.search(answer) for pattern in UNSUPPORTED_EQUIPMENT_PATTERNS):
        return False
    if _context_has_medical_restrictions(context):
        return not any(pattern.search(answer) for pattern in MEDICAL_CONSULTATION_FORBIDDEN_PATTERNS)
    return True


def validator_intercept_count() -> int:
    return _intercept_count


def clean_template_answer(context: dict[str, Any]) -> str:
    """собирает безопасный ответ обычным языком из разрешённых полей контекста."""

    message_to_user = context.get("message_to_user")
    if isinstance(message_to_user, str) and message_to_user.strip():
        return message_to_user.strip()

    suggested_services = context.get("suggested_services")
    if isinstance(suggested_services, list) and suggested_services:
        names = [
            str(service.get("name"))
            for service in suggested_services
            if isinstance(service, dict) and service.get("name")
        ]
        if names:
            return (
                "Для такого запроса обычно подходят: "
                + ", ".join(names)
                + ". Точные рекомендации даст специалист на консультации."
            )

    service = context.get("service") if isinstance(context.get("service"), dict) else {}
    price = context.get("price") if isinstance(context.get("price"), dict) else {}
    phrasebook = context.get("phrasebook") if isinstance(context.get("phrasebook"), dict) else {}
    price_disclaimer = str(
        phrasebook.get("price_disclaimer")
        or "Предварительно так, точнее сообщит специалист."
    )
    parts: list[str] = []
    if service.get("name"):
        description = service.get("short_description")
        if description:
            parts.append(f"{service['name']} — {description}")
        else:
            parts.append(str(service["name"]))
    if price.get("price_text"):
        parts.append(f"Стоимость: {price['price_text']}. {price_disclaimer}")
    if parts:
        return " ".join(parts)
    return "Уточните, пожалуйста, что вас интересует? Могу рассказать про услуги, цены или записать к специалисту."


def fallback_after_invalid_response(answer: str, context: dict[str, Any]) -> str:
    """логирует перехват валидатором и возвращает детерминированный чистый ответ."""

    global _intercept_count
    _intercept_count += 1
    logger.warning("response validator intercepted raw context leak count=%s answer=%r", _intercept_count, answer[:300])
    return clean_template_answer(context)
