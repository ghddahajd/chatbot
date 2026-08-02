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
        r"ipl-?лазер|неодимов|эрбиев|palo(?:mar)?|vectus|sciton|joule|"
        r"forever\s+clear|candela|gentlelase|m22|bbl|futura|exilis|"
        r"onetec|surgitron)",
        re.IGNORECASE,
    ),
)
UNSUPPORTED_EFFICACY_CLAIM_PATTERNS = (
    re.compile(
        r"(?:помогает\s+от|лечит|лечить|разрушает\s+бактерии|эффективен\s+(?:при|от)|эффективна\s+(?:при|от))",
        re.IGNORECASE,
    ),
)
CONSULTATION_FORBIDDEN_PATTERNS = (
    *RAW_CONTEXT_PATTERNS,
    re.compile(r"\d"),
    re.compile(r"(?:₽|руб|рублей)", re.IGNORECASE),
    *UNSUPPORTED_DETAIL_PATTERNS,
    *UNSUPPORTED_EFFICACY_CLAIM_PATTERNS,
)
MEDICAL_CONSULTATION_FORBIDDEN_PATTERNS = (
    re.compile(
        r"(?:диагноз|лечени|лечить|препарат|таблет|мазь|антибиотик|назнач|"
        r"гарант|безопасн|побочн|противопоказ|симптом|аллерг|беремен|"
        r"кров|родин|рубц|осложнен|осложнён|нормально|опасн|покраснен|от[её]к)",
        re.IGNORECASE,
    ),
)
ARTICLE_GUIDANCE_FORBIDDEN_PATTERNS = (
    *RAW_CONTEXT_PATTERNS,
    re.compile(r"(?:₽|руб(?:л|\.))", re.IGNORECASE),
    re.compile(
        r"(?:вам\s+нужно|вам\s+подойд[её]т|вам\s+подходят|"
        r"рекомендую|мы\s+рекомендуем|советую|назначаю|назначить|"
        r"диагноз|гарантированн|гарантируем|точно\s+поможет|"
        r"избавит|вылечит)",
        re.IGNORECASE,
    ),
    *UNSUPPORTED_EQUIPMENT_PATTERNS,
    *UNSUPPORTED_EFFICACY_CLAIM_PATTERNS,
)
FAQ_ALLOWED_WORDS = {
    "данным",
    "статьи",
    "статья",
    "указано",
    "подробности",
    "уточнит",
    "уточнить",
    "менеджер",
    "менеджеру",
    "специалист",
    "специалисту",
    "важно",
    "лучше",
    "вопросу",
    "индивидуально",
}


def _digit_groups(value: str) -> set[str]:
    return {re.sub(r"\D+", "", group) for group in re.findall(r"\d[\d\s]*", value)}


def _digits(value: str) -> str:
    return re.sub(r"\D+", "", value)


def _significant_words(value: str) -> set[str]:
    normalized = re.sub(r"[^a-zа-я0-9\s]+", " ", value.lower().replace("ё", "е"))
    stop_words = {"услуга", "услуги", "центр", "специалист", "процедура", "процедуры"}
    return {word for word in normalized.split() if len(word) >= 5 and word not in stop_words}


def _brand_like_tokens(text: str) -> set[str]:
    """Слова с заглавной буквы НЕ в начале предложения — типичный паттерн для названий
    брендов/препаратов. LLM иногда придумывает несуществующие бренды (опечатки/галлюцинации
    вроде "Миктоца") или называет реально существующий, но незаявленный бренд (например
    "Диспорт") — оба случая выглядят одинаково: заглавная буква не на старте предложения."""

    tokens: set[str] = set()
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        words = sentence.split()
        for index, raw_word in enumerate(words):
            cleaned = re.sub(r"[^A-Za-zА-Яа-яЁё-]", "", raw_word)
            if index == 0 or len(cleaned) < 4:
                continue
            if cleaned[0].isupper() and not cleaned.isupper():
                tokens.add(cleaned.lower().replace("ё", "е"))
    return tokens


def _undisclosed_equipment_pattern(context: dict[str, Any]) -> re.Pattern[str] | None:
    """Динамический, per-клиентский аналог UNSUPPORTED_EQUIPMENT_PATTERNS — построен из
    context["undisclosed_equipment_terms"] (см. policy.undisclosed_equipment_terms()), а не из
    захардкоженного generic-списка. Захардкоженный список писался не под конкретных клиентов и
    не содержит, например, реальный закрытый бренд ROSH ("InMode Morpheus8") — эта проверка
    закрывает именно тот пробел."""

    terms = context.get("undisclosed_equipment_terms")
    if not isinstance(terms, list):
        return None
    escaped = sorted(
        (re.escape(str(term)) for term in terms if str(term).strip()),
        key=len,
        reverse=True,
    )
    if not escaped:
        return None
    return re.compile(r"(?:" + "|".join(escaped) + r")", re.IGNORECASE)


def _article_context_answer(context: dict[str, Any]) -> str:
    article_context = context.get("article_context")
    if not isinstance(article_context, list) or not article_context:
        return ""

    first_match = article_context[0]
    if not isinstance(first_match, dict):
        return ""

    title = str(first_match.get("title") or "статьи").strip()
    snippet = re.sub(r"\s+", " ", str(first_match.get("snippet") or "")).strip(" .")
    if not snippet:
        return ""
    if len(snippet) > 360:
        cutoff = snippet.rfind(" ", 0, 360)
        snippet = snippet[: cutoff if cutoff > 240 else 360].strip(" ,;:—-") + "..."
    return f"По данным статьи «{title}»: {snippet}. Подробности уточнит менеджер."


def _validate_fact_constraints(answer: str, context: dict[str, Any]) -> bool:
    question_type = context.get("question_type")
    service = context.get("service") if isinstance(context.get("service"), dict) else {}
    price = context.get("price") if isinstance(context.get("price"), dict) else {}
    phrasebook = context.get("phrasebook") if isinstance(context.get("phrasebook"), dict) else {}
    price_disclaimer = str(
        phrasebook.get("price_disclaimer")
        or "Это предварительная стоимость. Точную сумму подтвердит менеджер после уточнения деталей."
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
        grounding_text = f"{snippets_text} {context.get('user_message') or ''}"
        safe_words = _significant_words(grounding_text)
        if safe_words and not (_significant_words(answer) & safe_words):
            return False
        answer_words = _significant_words(answer)
        ungrounded_words = answer_words - safe_words - FAQ_ALLOWED_WORDS
        if any("токс" in word for word in ungrounded_words):
            return False
        # только реальный источник (статьи), НЕ user_message — иначе пользователь может сам
        # назвать бренд в вопросе и тем самым "разблокировать" его в ответе модели.
        brand_safe_words = _significant_words(snippets_text)
        ungrounded_brand_words = _brand_like_tokens(answer) - _brand_like_tokens(snippets_text) - brand_safe_words
        if ungrounded_brand_words:
            return False
        if any(pattern.search(answer) for pattern in UNSUPPORTED_EQUIPMENT_PATTERNS):
            return False
        undisclosed_pattern = _undisclosed_equipment_pattern(context)
        if undisclosed_pattern and undisclosed_pattern.search(answer):
            return False
        if any(pattern.search(answer) for pattern in UNSUPPORTED_EFFICACY_CLAIM_PATTERNS):
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
    undisclosed_pattern = _undisclosed_equipment_pattern(context or {})
    if undisclosed_pattern and undisclosed_pattern.search(answer):
        return False
    if _context_has_medical_restrictions(context):
        return not any(pattern.search(answer) for pattern in MEDICAL_CONSULTATION_FORBIDDEN_PATTERNS)
    return True


def validate_article_guidance_response(answer: str, context: dict[str, Any] | None = None) -> bool:
    """проверяет LLM-перефразировку approved article excerpt перед показом пользователю."""

    if not answer.strip():
        return False
    if any(pattern.search(answer) for pattern in ARTICLE_GUIDANCE_FORBIDDEN_PATTERNS):
        return False
    if context is None:
        return False
    undisclosed_pattern = _undisclosed_equipment_pattern(context)
    if undisclosed_pattern and undisclosed_pattern.search(answer):
        return False

    guidance = context.get("article_guidance_candidate")
    guidance = guidance if isinstance(guidance, dict) else {}
    grounding_text = " ".join(
        [
            str(guidance.get("excerpt") or ""),
            str(guidance.get("service_names") or ""),
        ]
    )
    safe_words = _significant_words(grounding_text)
    return not safe_words or bool(_significant_words(answer) & safe_words)


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
                + ". Точные рекомендации даст менеджер на консультации."
            )

    if context.get("question_type") == "faq_question":
        article_answer = _article_context_answer(context)
        if article_answer:
            return article_answer

    service = context.get("service") if isinstance(context.get("service"), dict) else {}
    price = context.get("price") if isinstance(context.get("price"), dict) else {}
    phrasebook = context.get("phrasebook") if isinstance(context.get("phrasebook"), dict) else {}
    price_disclaimer = str(
        phrasebook.get("price_disclaimer")
        or "Это предварительная стоимость. Точную сумму подтвердит менеджер после уточнения деталей."
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
    return "Уточните, пожалуйста, что вас интересует? Могу рассказать про услуги, цены или записать к менеджеру."


def fallback_after_invalid_response(answer: str, context: dict[str, Any]) -> str:
    """логирует перехват валидатором и возвращает детерминированный чистый ответ."""

    global _intercept_count
    _intercept_count += 1
    logger.warning("response validator intercepted raw context leak count=%s answer=%r", _intercept_count, answer[:300])
    return clean_template_answer(context)
