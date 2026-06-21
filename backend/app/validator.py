"""валидация ответов и детерминированные резервные ответы."""

from __future__ import annotations

import logging
import re
from typing import Any


logger = logging.getLogger(__name__)
_intercept_count = 0

RAW_CONTEXT_PATTERNS = (
    re.compile(r"\bservice_id\b", re.IGNORECASE),
    re.compile(r"\bprice_text\b", re.IGNORECASE),
    re.compile(r"\bsafe_context\b", re.IGNORECASE),
    re.compile(r"\bshort_description\b", re.IGNORECASE),
    re.compile(r"\bquestion_type\b", re.IGNORECASE),
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
CONSULTATION_FORBIDDEN_PATTERNS = (
    *RAW_CONTEXT_PATTERNS,
    re.compile(r"\d"),
    re.compile(r"(?:₽|руб|рублей)", re.IGNORECASE),
    re.compile(
        r"(?:диагноз|лечени|лечить|препарат|таблет|мазь|антибиотик|назнач|"
        r"гарант|безопасн|побочн|противопоказ|симптом|аллерг|беремен|"
        r"кров|родин|осложнен|осложнён|нормально|опасн|покраснен|от[её]к)",
        re.IGNORECASE,
    ),
    *UNSUPPORTED_DETAIL_PATTERNS,
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


def validate_consultation_response(answer: str) -> bool:
    """более мягкая защита для консультационных ответов только от llm."""

    if not answer.strip():
        return False
    return not any(pattern.search(answer) for pattern in CONSULTATION_FORBIDDEN_PATTERNS)


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
    parts: list[str] = []
    if service.get("name"):
        description = service.get("short_description")
        if description:
            parts.append(f"{service['name']} — {description}")
        else:
            parts.append(str(service["name"]))
    if price.get("price_text"):
        parts.append(f"Стоимость: {price['price_text']}. Предварительно так, точнее сообщит специалист.")
    if parts:
        return " ".join(parts)
    return "Уточните, пожалуйста, что вас интересует? Могу рассказать про услуги, цены или записать к специалисту."


def fallback_after_invalid_response(answer: str, context: dict[str, Any]) -> str:
    """логирует перехват валидатором и возвращает детерминированный чистый ответ."""

    global _intercept_count
    _intercept_count += 1
    logger.warning("response validator intercepted raw context leak count=%s answer=%r", _intercept_count, answer[:300])
    return clean_template_answer(context)
