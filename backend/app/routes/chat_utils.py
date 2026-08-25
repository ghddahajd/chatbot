"""вспомогательные функции для роутов чата."""

from __future__ import annotations

import logging
import re
import time
from uuid import uuid4

from fastapi import Request

from ..knowledge import KnowledgeBase
from ..llm import MockLLMClient
from ..knowledge import normalize_text
from ..models import Message, MessageRole, PolicyAction, PolicyReason, PolicyResult, QuickAction, Session
from ..policy import classify_and_extract
from ..policy.adapter import merge_policy_classifications, structured_to_policy_classification
from ..policy.constants import (
    AFFIRMATIVE_MESSAGES,
    BENIGN_MEDICAL_KEYWORDS,
    BOOKING_KEYWORDS,
    BOT_IDENTITY_SIGNAL_KEYWORDS,
    BOT_IDENTITY_WHO_ARE_YOU_PHRASES,
    CLARIFY_SHORT_MESSAGES,
    COMPETITOR_CONTEXT_TOKENS,
    COMPETITOR_PRICE_NUMBER_PATTERN,
    COMPETITOR_PRICE_TOKEN,
    DURATION_KEYWORDS,
    EXPLANATION_KEYWORDS,
    GUARANTEE_REQUEST_KEYWORDS,
    HESITATION_EXACT_MESSAGES,
    HESITATION_KEYWORDS,
    MEDICAL_KEYWORDS,
    PAIN_FEAR_ANTICIPATION_KEYWORDS,
    PAIN_OR_SIDE_EFFECT_KEYWORDS,
    PRICE_KEYWORDS,
    PRICE_OBJECTION_KEYWORDS,
    PRICE_OBJECTION_TOKENS,
)
from ..policy.extractors import (
    contains_exact_token,
    contains_keyword,
    contains_keyword_word_start,
    extract_phone,
    fuzzy_contains,
    last_service_from_history,
    mentions_company_city,
    strip_anaphoric_pronouns,
)
from ..policy.restricted import (
    get_restricted_categories,
    has_medical_restricted_category,
    is_restricted_question,
)
from ..policy.variants import (
    find_variant_matches,
    is_variant_list_question,
    should_stay_in_service_context,
)


fallback_llm_client = MockLLMClient()
logger = logging.getLogger(__name__)
MAX_SESSION_MESSAGES = 30
MAX_MESSAGE_LENGTH = 1000
HAS_LETTER_OR_DIGIT = re.compile(r"[0-9A-Za-zА-Яа-яЁё]")
RATE_LIMIT_ANSWER = (
    "Похоже, разговор затянулся. Если остались вопросы — оставьте телефон, "
    "мы свяжемся, или попробуйте начать новый диалог."
)
CONSULTATION_RISK_RESTRICTED = "RESTRICTED"
CONSULTATION_RISK_SAFE = "SAFE"


def format_quick_actions(
    labels: list[object],
    request: Request,
    knowledge_base: KnowledgeBase | None = None,
) -> list[QuickAction]:
    company = (knowledge_base or request.app.state.knowledge_base).company
    values_by_label = {
        "Позвать менеджера": ("message", "Хочу поговорить с менеджером"),
        "Посмотреть услуги": ("message", "Покажи список услуг"),
        "Оставить телефон": ("message", "Хочу оставить телефон"),
        "Уточнить цену": ("message", "Хочу уточнить цену"),
        "Написать в Telegram": ("link", company.telegram_url or ""),
        "Открыть сайт": ("link", company.website_url or ""),
    }
    normalized_values = {label.casefold(): value for label, value in values_by_label.items()}

    actions: list[QuickAction] = []
    for item in labels:
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            action_type = str(item.get("type") or "message").strip()
            value = str(item.get("value") or label).strip()
            if label and value:
                actions.append(QuickAction(label=label, type=action_type, value=value))
            continue

        label = str(item)
        action_type, value = normalized_values.get(label.casefold(), ("message", label))
        if action_type == "link" and not value:
            continue
        actions.append(QuickAction(label=label, type=action_type, value=value))
    return actions


def service_classifier_payload(
    request: Request,
    knowledge_base: KnowledgeBase | None = None,
    *,
    include_variants: bool = False,
) -> list[dict[str, object]]:
    """include_variants по умолчанию False: этот payload уходит и в промпт настоящего LLM
    (classify_structured/classify_and_extract в resolve_classification) — добавлять сюда
    все variants (до 351 строки по всем услугам) раздуло бы токены/стоимость КАЖДОГО
    сообщения. True — только для мест, где payload идёт исключительно в локальный
    (бесплатный) classify_and_extract, а не в LLM."""

    selected_knowledge_base = knowledge_base or request.app.state.knowledge_base
    return [
        {
            "id": service.id,
            "name": service.name,
            "synonyms": service.synonyms,
            "category": service.category,
            **({"variants": service.variants} if include_variants else {}),
        }
        for service in selected_knowledge_base.services
    ]


def _last_assistant_text(session: Session) -> str:
    for message in reversed(session.messages[:-1]):
        if message.role == MessageRole.ASSISTANT:
            return normalize_text(message.text)
    return ""


def maybe_contextual_classification(
    message: str,
    session: Session,
) -> dict[str, object] | None:
    normalized_message = normalize_text(message)
    last_assistant_text = _last_assistant_text(session)
    if normalized_message in {"что еще", "что ещё", "а что еще", "а что ещё"} and (
        "доступны услуги" in last_assistant_text
        or "список услуг" in last_assistant_text
        or "услуги:" in last_assistant_text
    ):
        return {"intent": "list_services", "service_id": None, "confidence": 0.88}

    if normalized_message not in AFFIRMATIVE_MESSAGES:
        return None

    if (
        "могу рассказать про услуги" in last_assistant_text
        or "давайте я расскажу про наши услуги" in last_assistant_text
        or "услуги и цены" in last_assistant_text
    ):
        return {"intent": "list_services", "service_id": None, "confidence": 0.9}

    return None


def contextual_affirmative_response(
    message: str,
    session: Session,
) -> PolicyResult | None:
    normalized_message = normalize_text(message)
    if normalized_message not in AFFIRMATIVE_MESSAGES:
        return None
    # Живой баг (2026-08-10): эта функция перехватывает ЛЮБОЕ голое "давай"/"да" раньше, чем
    # сообщение доходит до policy — включая случай, когда бот сам только что явно предложил
    # "расскажу подробнее, что входит?" (objection_price). Пользователь подтверждает ИМЕННО
    # это предложение, а получал общий "что уточнить: цену, детали или менеджера?", будто
    # ничего не предлагалось. Если последний ответ бота был objection_handled — не перехватываем
    # здесь, пропускаем дальше в analyze_message, где это уже обрабатывается прицельно.
    if session.last_intent == PolicyReason.OBJECTION_HANDLED.value:
        return None

    for history_message in reversed(session.messages[:-1]):
        if history_message.role != MessageRole.USER:
            continue
        if normalize_text(history_message.text) in AFFIRMATIVE_MESSAGES:
            continue
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.OK,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": "Что уточнить по этой услуге: цену, детали или соединить с менеджером?"
            },
            quick_actions=[
                "Уточнить цену",
                {
                    "label": "Подробнее",
                    "type": "message",
                    "value": "Расскажи подробнее про эту услугу",
                },
                "Позвать менеджера",
            ],
        )

    return None


def should_ignore_model_location_mismatch(
    message: str,
    company_city: str,
    local_result: dict[str, object],
    model_result: dict[str, object],
) -> bool:
    """не даёт модели считать mismatch фразы вроде 'я в Москве'."""

    if str(model_result.get("intent") or "") != "location_mismatch":
        return False
    if str(local_result.get("intent") or "") == "location_mismatch":
        return False
    return mentions_company_city(normalize_text(message), company_city)


def should_ignore_model_regulated_advice(
    local_result: dict[str, object],
    model_result: dict[str, object],
    domain_profile: dict[str, object],
) -> bool:
    """не даёт generic/auto домену принять medical-like risk от модели."""

    model_intent = str(model_result.get("intent") or "")
    local_intent = str(local_result.get("intent") or "")
    return (
        model_intent in {"medical_advice", "regulated_advice"}
        and local_intent not in {"medical_advice", "regulated_advice"}
        and not has_medical_restricted_category(domain_profile)
    )


def _clarify_without_model(message: str) -> dict[str, object] | None:
    normalized_message = normalize_text(message)
    if not HAS_LETTER_OR_DIGIT.search(message):
        return {"intent": "clarify", "service_id": None, "confidence": 0.9}
    if (
        normalized_message in AFFIRMATIVE_MESSAGES
        or normalized_message in CLARIFY_SHORT_MESSAGES
        or len(normalized_message) <= 2
    ):
        return {"intent": "clarify", "service_id": None, "confidence": 0.82}
    if "метро" in normalized_message or "рядом" in normalized_message:
        return {"intent": "clarify", "service_id": None, "confidence": 0.78}
    return None


def _contextual_service_classification(
    message: str,
    session: Session | None,
    knowledge_base: KnowledgeBase,
    local_result: dict[str, object],
) -> dict[str, object] | None:
    if session is None:
        return None

    service_id = session.last_service_id or last_service_from_history(session, knowledge_base)
    if not service_id:
        return None

    local_service_id = local_result.get("service_id")
    if local_service_id and local_service_id != service_id:
        return local_result

    normalized_message = normalize_text(message)
    if fuzzy_contains(normalized_message, PRICE_KEYWORDS) or normalized_message in {"а сколько", "сколько", "почем", "почём"}:
        return {"intent": "price_question", "service_id": service_id, "confidence": 0.9}
    if contains_keyword(normalized_message, DURATION_KEYWORDS) or contains_keyword(
        normalized_message,
        EXPLANATION_KEYWORDS,
    ):
        return {"intent": str(local_result.get("intent") or "service_mention"), "service_id": service_id, "confidence": 0.86}
    return None


FACT_VALUE_FOLLOWUP_KEYWORDS = {
    "какие препараты",
    "а какие препараты",
    "какой препарат",
    "а какой препарат",
    "что используете",
    "а что используете",
    "чем работаете",
    "а чем работаете",
}
DOCTOR_FOLLOWUP_KEYWORDS = {
    "дерматолог",
    "гинеколог",
    "косметолог",
    "терапевт",
    "врач",
    "доктор",
}
# "кто" ловит "кто ведёт дерматологию", но не "покажи врачей"/"какие врачи есть" — без
# "кто" такие сообщения раньше уезжали на нестабильный LLM-классификатор. "покаж"/"покаж."
# — частая обрезанная форма набора ("покаж врачей") — без неё сообщение проваливается мимо
# doctor-ветки и нечётко цепляет услугу "Консультации" (в её синонимах есть голое "врач").
DOCTOR_INFO_TRIGGER_KEYWORDS = {
    "кто",
    "покажи",
    "покажите",
    "покаж",
    "какие",
    "какой",
    "список",
    "есть ли",
}
# §3.4 скрипта: "не понимаю, к кому мне лучше попасть" — пациент не просит список врачей
# (это DOCTOR_INFO_TRIGGER_KEYWORDS), а не может сам решить, к какому специалисту обратиться.
# Живой баг при разработке: жёсткие многословные фразы типа "к кому попасть" не матчили
# реальную формулировку "к кому МНЕ ЛУЧШЕ попасть" — вставленные слова рвут точную
# последовательность токенов. Матчим маркер неуверенности + паттерн выбора врача отдельными
# короткими фразами (по 2 токена), не одной длинной фразой — вставки между ними не мешают.
DOCTOR_UNCERTAINTY_MARKERS = {"не знаю", "не понимаю", "не пойму"}
DOCTOR_ROUTING_PATTERNS = {
    "к кому",
    "к какому",
    "какой врач",
    "какого врача",
    "какой специалист",
    "какого специалиста",
}


def _doctor_uncertainty_classification(message: str) -> dict[str, object] | None:
    normalized_message = normalize_text(message)
    has_routing = contains_keyword(normalized_message, DOCTOR_ROUTING_PATTERNS)
    if not has_routing:
        return None
    has_marker = contains_keyword(normalized_message, DOCTOR_UNCERTAINTY_MARKERS)
    # без явного "не знаю"/"не понимаю" — только прямой вопрос-выбор со словом "лучше"
    # ("к какому врачу лучше обратиться?"), не любое упоминание "к кому"/"какой врач".
    if not has_marker and "лучше" not in normalized_message:
        return None
    return {
        "intent": "doctor_uncertain",
        "service_id": None,
        "confidence": 0.9,
    }


def _active_frame(session: Session | None):
    if session is None or session.active_frame is None:
        return None
    frame = session.active_frame
    if frame.expires_at_turn is not None and session.message_count > frame.expires_at_turn:
        return None
    return frame


def _contextual_frame_classification(
    message: str,
    session: Session | None,
    local_result: dict[str, object],
    knowledge_base: KnowledgeBase | None = None,
) -> dict[str, object] | None:
    frame = _active_frame(session)
    if frame is None:
        return None

    local_intent = str(local_result.get("intent") or "")
    if local_intent in {"medical_advice", "regulated_advice", "operator_request", "location_mismatch"}:
        return None

    if frame.frame_type == "symptom_followup" and frame.entity_id:
        # Узкий, изолированный фрейм (живёт 1 шаг) под §3.2-гейт: локальный классификатор сам
        # не понял неопределённый ответ на уточняющий вопрос ("где-то полгода" -> unknown_service/
        # off_topic/faq_question) — подталкиваем к услуге, про которую только что спрашивали,
        # вместо дефолтного "не нашёл". Если классификатор уверенно понял что-то ДРУГОЕ (другая
        # услуга, запись, цена) — не трогаем, работает как обычно.
        local_service_id = str(local_result.get("service_id") or "").strip()
        if local_intent in {"unknown_service", "off_topic", "faq_question"} or (
            local_intent == "service_mention" and not local_service_id
        ):
            return {"intent": "service_mention", "service_id": frame.entity_id, "confidence": 0.85}
        return None

    normalized_message = normalize_text(message)
    if frame.frame_type == "clinic_info" and frame.slots.get("topic") == "doctors":
        if contains_keyword(normalized_message, DOCTOR_FOLLOWUP_KEYWORDS):
            return {
                "intent": "clinic_info",
                "service_id": None,
                "confidence": 0.9,
                "context_topic": "doctors",
            }

    if frame.frame_type == "cosmetic_candidates":
        # Симптом резолвился в НЕСКОЛЬКО кандидатов (см. _context_frame_from_policy_result) —
        # угадывать один нельзя (тот же принцип, что и в similar_services_result), но
        # follow-up вида "а сколько это стоит?" не должен проваливаться в общий "не нашёл",
        # раз мы только что явно предложили конкретные варианты.
        candidates = frame.slots.get("candidates")
        anaphora_free_message = strip_anaphoric_pronouns(normalized_message)
        is_relevant_followup = (
            fuzzy_contains(anaphora_free_message, PRICE_KEYWORDS)
            or contains_keyword(anaphora_free_message, DURATION_KEYWORDS)
            or contains_keyword(anaphora_free_message, BOOKING_KEYWORDS)
            or normalized_message in {
                "а сколько",
                "сколько",
                "почем",
                "почём",
                "а почем",
                "а почём",
            }
        )
        if isinstance(candidates, list) and candidates and is_relevant_followup:
            candidate_ids = [
                str(item.get("id"))
                for item in candidates
                if isinstance(item, dict) and item.get("id")
            ]
            if candidate_ids:
                return {
                    "intent": "cosmetic_concern",
                    "service_id": None,
                    "confidence": 0.9,
                    "context_candidate_service_ids": candidate_ids,
                }

    contextual_service = None
    if knowledge_base is not None and frame.entity_id:
        contextual_service = knowledge_base.find_service_by_id(frame.entity_id)

    if local_intent in {"unknown_service", "off_topic"}:
        if (
            frame.frame_type in {"service_interest", "fact_question"}
            and frame.entity_id
            and contextual_service is not None
        ):
            # Конкретное совпадение варианта важнее общего списка: слова вроде "зона"
            # входят и в общий вопрос ("какие зоны?"), и в названия вариантов ("Т зона").
            has_variant_match = bool(find_variant_matches(contextual_service, message, limit=1))
            if has_variant_match or is_variant_list_question(message):
                return {
                    "intent": "service_mention",
                    "service_id": frame.entity_id,
                    "confidence": 0.9,
                    "context_topic": "variant_lookup" if has_variant_match else "variants_list",
                }
        return None

    local_service_id = str(local_result.get("service_id") or "").strip()
    if local_service_id and frame.entity_id and local_service_id != frame.entity_id:
        # Живой баг (нагрузочный тест, 2026-08-25): "пилинг лица" в контексте "Лазерный
        # пилинг" переключался на другую услугу "Пилинги" вслепую — см. подробный разбор
        # в should_stay_in_service_context. Остаёмся в контексте ТОЛЬКО когда сообщение
        # ещё и упоминает саму текущую услугу, не просто общее слово вроде "лицо"/"зона" —
        # иначе сломали бы настоящие переключения темы ("а сколько чистка лица").
        if not should_stay_in_service_context(contextual_service, message):
            return local_result

    if frame.frame_type in {"service_interest", "fact_question"} and frame.entity_id:
        if local_intent == "booking_request":
            return {"intent": "booking_request", "service_id": frame.entity_id, "confidence": 0.9}
        frame_variant = frame.slots.get("variant") if isinstance(frame.slots.get("variant"), dict) else None
        if (
            frame_variant is not None
            and str(frame.slots.get("question_type") or "") == "variant_price"
            and (
                fuzzy_contains(strip_anaphoric_pronouns(normalized_message), PRICE_KEYWORDS)
                or normalized_message
                in {
                    "а сколько",
                    "сколько",
                    "почем",
                    "почём",
                    "а почем",
                    "а почём",
                    "а по деньгам",
                    "по деньгам",
                }
            )
        ):
            return {
                "intent": "service_mention",
                "service_id": frame.entity_id,
                "confidence": 0.92,
                "context_topic": "variant_repeat_price",
                "context_variant": frame_variant,
            }
        if contextual_service is not None and find_variant_matches(contextual_service, message, limit=1):
            return {
                "intent": "service_mention",
                "service_id": frame.entity_id,
                "confidence": 0.9,
                "context_topic": "variant_lookup",
            }
        if is_variant_list_question(message):
            return {
                "intent": "service_mention",
                "service_id": frame.entity_id,
                "confidence": 0.9,
                "context_topic": "variants_list",
            }
        anaphora_free_message = strip_anaphoric_pronouns(normalized_message)
        if fuzzy_contains(anaphora_free_message, PRICE_KEYWORDS) or normalized_message in {
            "а сколько",
            "сколько",
            "почем",
            "почём",
            "а почем",
            "а почём",
            "а все таки почем",
            "а всё таки почём",
            "а всё-таки почём",
            "а все-таки почем",
        }:
            return {"intent": "price_question", "service_id": frame.entity_id, "confidence": 0.92}
        if contains_keyword(anaphora_free_message, DURATION_KEYWORDS):
            return {"intent": "service_mention", "service_id": frame.entity_id, "confidence": 0.88}
        if contains_keyword(normalized_message, EXPLANATION_KEYWORDS) or normalized_message in {
            "а подробнее",
            "можно подробнее",
            "а можно подробнее",
            "а что это",
            "а что входит",
        }:
            return {"intent": "service_mention", "service_id": frame.entity_id, "confidence": 0.88}
        if contains_keyword(normalized_message, FACT_VALUE_FOLLOWUP_KEYWORDS):
            return {"intent": "service_mention", "service_id": frame.entity_id, "confidence": 0.9}

    return None


def _bot_identity_classification(message: str) -> dict[str, object] | None:
    """"ты бот?" — детерминированный ответ, не полагаемся на LLM: локальная модель ненадёжно
    следует нюансированным инструкциям (не путать с "хочу оператора"), см. structured-классификатор."""

    normalized_message = normalize_text(message)
    # contains_keyword матчит подстроку где угодно для ключей длиннее 3 символов — "боты"
    # внутри BOT_IDENTITY_SIGNAL_KEYWORDS ловил "работы"/"разработы" и т.п. как "ра"+"боты"
    # (живой баг, найден нагрузочным прогоном 2026-08-25: "какой у вас режим работы" уходило
    # в bot_identity вместо ответа про часы работы). word_start требует, чтобы ключ начинал
    # токен — та же защита, что уже стоит у MEDICAL_KEYWORDS/HARD_RESTRICTED_KEYWORDS.
    if not contains_keyword_word_start(
        normalized_message, BOT_IDENTITY_SIGNAL_KEYWORDS | BOT_IDENTITY_WHO_ARE_YOU_PHRASES
    ):
        return None
    return {
        "intent": "bot_identity",
        "service_id": None,
        "confidence": 0.95,
    }


def _pain_fear_objection_classification(message: str) -> dict[str, object] | None:
    """§4.5 скрипта: "боюсь, что будет больно/появятся побочные эффекты" — объекшен
    (тревога о ПРЕДСТОЯЩЕЙ процедуре), не медицинская эскалация. Раньше "больно" в
    MEDICAL_KEYWORDS перехватывало это на уровне local_result ДО того, как объекшены вообще
    проверялись (см. resolve_classification: строка с medical_advice/regulated_advice
    возвращает раньше _objection_classification). Требуем ОБА признака — глагол-опасение и
    слово о боли/побочках, — и явно исключаем случай, где есть ДРУГОЙ медицинский сигнал
    (например "боюсь, у меня кровит" — это не объекшен, реальный тревожный симптом остаётся
    приоритетным, безопасный дефолт как в BENIGN_MEDICAL_KEYWORDS)."""

    normalized_message = normalize_text(message)
    if not contains_keyword(normalized_message, PAIN_FEAR_ANTICIPATION_KEYWORDS):
        return None
    if not contains_keyword(normalized_message, PAIN_OR_SIDE_EFFECT_KEYWORDS):
        return None
    other_medical_signal = MEDICAL_KEYWORDS - PAIN_OR_SIDE_EFFECT_KEYWORDS - BENIGN_MEDICAL_KEYWORDS
    if contains_keyword(normalized_message, other_medical_signal):
        return None
    return {"intent": "objection", "service_id": None, "confidence": 0.88, "context_topic": "pain_fear"}


def _objection_classification(message: str) -> dict[str, object] | None:
    """Возражения по цене ("дорого"/"я подумаю"/"у конкурентов дешевле"/"гарантируете?") —
    детерминированный ответ по той же причине, что и bot_identity: локальная модель ненадёжно
    следует нюансированным инструкциям (не критиковать конкурентов, не обещать результат), а
    часть формулировок несёт юридический вес (ст.24 ФЗ "О рекламе", 323-ФЗ)."""

    normalized_message = normalize_text(message)
    if contains_keyword(normalized_message, PRICE_OBJECTION_KEYWORDS) or contains_exact_token(
        normalized_message, PRICE_OBJECTION_TOKENS
    ):
        return {"intent": "objection", "service_id": None, "confidence": 0.9, "context_topic": "price"}
    if contains_exact_token(normalized_message, COMPETITOR_CONTEXT_TOKENS) and (
        contains_exact_token(normalized_message, {COMPETITOR_PRICE_TOKEN})
        or COMPETITOR_PRICE_NUMBER_PATTERN.search(normalized_message)
    ):
        return {"intent": "objection", "service_id": None, "confidence": 0.9, "context_topic": "competitor"}
    if contains_keyword(normalized_message, GUARANTEE_REQUEST_KEYWORDS):
        return {"intent": "objection", "service_id": None, "confidence": 0.9, "context_topic": "guarantee"}
    if contains_keyword(normalized_message, HESITATION_KEYWORDS) or normalized_message in HESITATION_EXACT_MESSAGES:
        return {"intent": "objection", "service_id": None, "confidence": 0.88, "context_topic": "hesitation"}
    return None


def _doctor_info_classification(message: str) -> dict[str, object] | None:
    normalized_message = normalize_text(message)
    if not contains_keyword(normalized_message, DOCTOR_INFO_TRIGGER_KEYWORDS):
        return None
    if not contains_keyword(normalized_message, DOCTOR_FOLLOWUP_KEYWORDS):
        return None
    return {
        "intent": "clinic_info",
        "service_id": None,
        "confidence": 0.88,
        "context_topic": "doctors",
    }


def _looks_like_unknown_service_question(message: str) -> bool:
    normalized_message = normalize_text(message)
    return any(
        phrase in normalized_message
        for phrase in {
            "хочу ",
            "есть",
            "делаете",
            "делаите",
            "можно",
            "сколько стоит",
            "цена",
        }
    )


async def resolve_classification(
    message: str,
    request: Request,
    knowledge_base: KnowledgeBase | None = None,
    session: Session | None = None,
) -> dict[str, object]:
    selected_knowledge_base = knowledge_base or request.app.state.knowledge_base
    known_services = service_classifier_payload(request, selected_knowledge_base)
    # variants только в этот, ЛОКАЛЬНЫЙ вызов — known_services (без variants) ниже отдельно
    # уходит в промпт настоящего LLM (classify_structured/classify_and_extract), это не трогаем.
    local_result = classify_and_extract(
        message,
        service_classifier_payload(request, selected_knowledge_base, include_variants=True),
        selected_knowledge_base.company.city,
        selected_knowledge_base.domain_profile,
    )
    local_intent = str(local_result.get("intent") or "")
    local_confidence = float(local_result.get("confidence") or 0.0)
    if local_intent in {"medical_advice", "regulated_advice"}:
        pain_fear_result = _pain_fear_objection_classification(message)
        if pain_fear_result is not None:
            return pain_fear_result
        return local_result
    if extract_phone(message):
        return local_result
    bot_identity_result = _bot_identity_classification(message)
    if bot_identity_result is not None:
        return bot_identity_result
    objection_result = _objection_classification(message)
    if objection_result is not None:
        return objection_result
    doctor_uncertainty_result = _doctor_uncertainty_classification(message)
    if doctor_uncertainty_result is not None:
        return doctor_uncertainty_result
    doctor_info_result = _doctor_info_classification(message)
    if doctor_info_result is not None:
        return doctor_info_result
    clarify_result = _clarify_without_model(message)
    if clarify_result is not None:
        return clarify_result
    frame_result = _contextual_frame_classification(message, session, local_result, selected_knowledge_base)
    if frame_result is not None:
        return frame_result
    contextual_result = _contextual_service_classification(message, session, selected_knowledge_base, local_result)
    if contextual_result is not None:
        return contextual_result

    settings = request.app.state.settings
    skip_local_classifier = settings.llm_skip_classifier_for_local
    if skip_local_classifier is None:
        skip_local_classifier = settings.llm_provider.lower().strip() == "openai_compatible"
    if (
        not settings.llm_use_structured_classifier
        and settings.llm_provider.lower().strip() == "openai_compatible"
        and skip_local_classifier
        and local_confidence > 0
    ):
        return local_result

    structured_result = None
    if settings.llm_use_structured_classifier:
        try:
            structured_result = await request.app.state.llm_client.classify_structured(
                message,
                known_services,
                selected_knowledge_base.domain_profile,
            )
        except Exception as error:
            logger.info("structured_classifier_source=local reason=helper_error error=%s", type(error).__name__)
            return local_result

    if structured_result is None:
        try:
            model_result = await request.app.state.llm_client.classify_and_extract(message, known_services)
        except Exception as error:
            logger.info("classifier_source=local reason=helper_error error=%s", type(error).__name__)
            return local_result
    else:
        model_result = structured_to_policy_classification(structured_result)

    if should_ignore_model_regulated_advice(local_result, model_result, selected_knowledge_base.domain_profile):
        return local_result

    if should_ignore_model_location_mismatch(
        message,
        selected_knowledge_base.company.city,
        local_result,
        model_result,
    ):
        return local_result
    if (
        str(model_result.get("intent") or "") == "off_topic"
        and local_intent == "service_mention"
        and not local_result.get("service_id")
        and _looks_like_unknown_service_question(message)
    ):
        return local_result

    return merge_policy_classifications(local_result, model_result)


async def safe_small_talk(request: Request, company_name: str, message: str) -> str:
    try:
        return await request.app.state.llm_client.small_talk(company_name, message)
    except Exception as error:
        logger.info("small_talk_source=fallback reason=helper_error error=%s", type(error).__name__)
        return await fallback_llm_client.small_talk(company_name, message)


def should_use_consultation_llm(context: dict[str, object]) -> bool:
    if context.get("question_type") == "cosmetic_concern":
        return True
    if context.get("question_type"):
        return False
    if context.get("message_to_user"):
        return False
    service = context.get("service")
    return isinstance(service, dict) and bool(service.get("name"))


async def classify_consultation_risk(
    request: Request,
    message: str,
    context: dict[str, object],
) -> tuple[str, str]:
    """классифицирует риск консультационного ответа относительно domain_profile."""

    request_id = uuid4().hex[:10]
    started_at = time.perf_counter()
    if context.get("question_type") == "cosmetic_concern":
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "request_id=%s classification_restricted_ms=%.1f result=%s source=policy_cosmetic_concern",
            request_id,
            elapsed_ms,
            CONSULTATION_RISK_SAFE,
        )
        return CONSULTATION_RISK_SAFE, request_id

    domain_profile = context.get("domain_profile") if isinstance(context.get("domain_profile"), dict) else {}
    categories = get_restricted_categories(domain_profile)
    if not categories:
        return CONSULTATION_RISK_SAFE, request_id

    is_restricted, restricted_category = is_restricted_question(message, domain_profile)
    if is_restricted:
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "request_id=%s classification_restricted_ms=%.1f result=%s category=%s source=local",
            request_id,
            elapsed_ms,
            CONSULTATION_RISK_RESTRICTED,
            restricted_category,
        )
        return CONSULTATION_RISK_RESTRICTED, request_id

    if not has_medical_restricted_category(domain_profile):
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        logger.info(
            "request_id=%s classification_restricted_ms=%.1f result=%s categories=%s source=policy",
            request_id,
            elapsed_ms,
            CONSULTATION_RISK_SAFE,
            ",".join(categories),
        )
        return CONSULTATION_RISK_SAFE, request_id

    local_result = await fallback_llm_client.classify_restricted_risk(message)
    service = context.get("service")
    has_service_context = isinstance(service, dict) and bool(service.get("name"))
    if local_result == "MEDICAL" or has_service_context:
        result = local_result
    else:
        try:
            result = await request.app.state.llm_client.classify_restricted_risk(message)
        except Exception as error:
            logger.info("restricted_classifier_source=local reason=helper_error error=%s", type(error).__name__)
            result = local_result

    normalized_result = str(result or "").strip().upper()
    if normalized_result not in {"MEDICAL", "COSMETIC"}:
        normalized_result = "MEDICAL"
    risk = CONSULTATION_RISK_RESTRICTED if normalized_result == "MEDICAL" else CONSULTATION_RISK_SAFE

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    logger.info(
        "request_id=%s classification_restricted_ms=%.1f result=%s legacy_result=%s category=medical",
        request_id,
        elapsed_ms,
        risk,
        normalized_result,
    )
    return risk, request_id


async def safe_restricted_handoff(request: Request, message: str) -> str:
    """возвращает безопасный handoff-ответ для restricted тем."""

    del request, message
    return await fallback_llm_client.restricted_handoff("")


async def safe_complete(
    request: Request,
    context: dict[str, object],
    message: str,
    history: list[Message],
) -> str:
    if should_use_consultation_llm(context):
        try:
            return await request.app.state.llm_client.service_consultation(
                context,
                message,
                history,
            )
        except Exception as error:
            logger.info("service_consultation_source=fallback reason=helper_error error=%s", type(error).__name__)
            return await fallback_llm_client.service_consultation(
                context,
                message,
                history,
            )

    try:
        return await request.app.state.llm_client.complete(
            request.app.state.system_prompt,
            context,
            message,
            history,
        )
    except Exception as error:
        logger.info("complete_source=fallback reason=helper_error error=%s", type(error).__name__)
        return await fallback_llm_client.complete(
            request.app.state.system_prompt,
            context,
            message,
            history,
        )
