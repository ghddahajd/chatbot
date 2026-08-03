"""защитный слой политики, который выполняется до любого вызова llm."""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..knowledge import KnowledgeBase, _token_prefix_match, normalize_text, phrasebook_value_to_text
from ..models import PendingAction, PolicyAction, PolicyReason, PolicyResult, Session
from ..services.rag_search import retrieve_article_context
from .constants import (
    BODY_TOPIC_SIGNAL_KEYWORDS,
    BOOKING_KEYWORDS,
    AMBULANCE_ACTION_KEYWORDS,
    AMBULANCE_SUBJECT_KEYWORDS,
    CLINIC_LOCATION_KEYWORDS,
    DEFAULT_SENSITIVE_TOPIC_KEYWORDS,
    DMS_FACT_KEYWORDS,
    DOCTOR_INFO_KEYWORDS,
    DOCTOR_SCHEDULE_KEYWORDS,
    DURATION_KEYWORDS,
    EQUIPMENT_QUESTION_KEYWORDS,
    EFFICACY_CLAIM_KEYWORDS,
    EXPLANATION_KEYWORDS,
    GENERIC_DOCTOR_LIST_KEYWORDS,
    GENERIC_PRICE_MESSAGES,
    LEAD_REQUEST_KEYWORDS,
    LEAD_FOLLOWUP_SHORT_KEYWORDS,
    BENIGN_MEDICAL_KEYWORDS,
    MEDICAL_KEYWORDS,
    MEDICAL_REFERRAL_KEYWORDS,
    NEGATIVE_MESSAGES,
    OMS_FACT_KEYWORDS,
    OPERATOR_REQUEST_KEYWORDS,
    PRICE_KEYWORDS,
    PRODUCTS_FACT_KEYWORDS,
    TELEGRAM_KEYWORDS,
    VISIT_KEYWORDS,
    WEBSITE_KEYWORDS,
)
from .extractors import (
    contains_keyword,
    extract_name,
    extract_person_lemma_via_ner,
    extract_phone,
    find_unsupported_city,
    fuzzy_contains,
    is_location_mismatch,
    last_service_from_history,
    lemmatize_known_name,
)
from .intent import classify_and_extract, normalize_classification
from .quick_actions import all_services_context, service_name_quick_actions, services_summary
from .restricted import is_restricted_question
from .rules import (
    city_prepositional,
    cosmetic_concern_services,
    mentions_unknown_service,
    similar_services_result,
)
from .variants import find_variant_matches, is_variant_list_question, variant_list_labels, variant_price_line


logger = logging.getLogger(__name__)


WIDE_PRICE_RANGE_RATIO = 3
BARE_SERVICE_MENTION_BLOCK_WORDS = {
    "а",
    "в",
    "вы",
    "есть",
    "делаете",
    "делаешь",
    "как",
    "когда",
    "колите",
    "можно",
    "на",
    "нужно",
    "подскажите",
    "почему",
    "почем",
    "почём",
    "сколько",
    "скажите",
    "стоимость",
    "стоит",
    "хочу",
    "цена",
    "что",
    "зачем",
    "записаться",
    "запишите",
}


def _phrase_seed(session: Session, key: str) -> str:
    return f"{session.session_id}:{key}:{session.message_count}"


def _phrase(knowledge_base: KnowledgeBase, key: str, seed: str | None = None) -> str:
    value = getattr(knowledge_base, "phrasebook", {}).get(key)
    return phrasebook_value_to_text(value, seed=seed)


def _format_phrase(knowledge_base: KnowledgeBase, key: str, seed: str | None = None, **values: object) -> str:
    phrase = _phrase(knowledge_base, key, seed=seed)
    if not phrase:
        return ""
    try:
        return phrase.format(**values)
    except (KeyError, ValueError):
        return phrase


def _looks_like_bare_service_mention(normalized_message: str) -> bool:
    tokens = normalized_message.split()
    if not tokens or len(tokens) > 5:
        return False
    return not any(token in BARE_SERVICE_MENTION_BLOCK_WORDS for token in tokens)


def _unknown_service_display_name(message: str) -> str:
    return message.strip().strip(" \t\r\n?!.,;:«»\"'")


def _unknown_service_message(
    knowledge_base: KnowledgeBase,
    session: Session,
    message: str,
    normalized_message: str,
) -> str:
    if _looks_like_bare_service_mention(normalized_message):
        display_name = _unknown_service_display_name(message)
        if display_name:
            return _format_phrase(
                knowledge_base,
                "unknown_service_named",
                seed=_phrase_seed(session, "unknown_service_named"),
                service=display_name,
            )
    return _phrase(knowledge_base, "unknown_service", seed=_phrase_seed(session, "unknown_service"))


def _service_link_action(service) -> dict[str, str] | None:
    page_url = str(getattr(service, "page_url", "") or "").strip()
    if not page_url:
        return None
    return {"label": "Перейти к услуге", "type": "link", "value": page_url}


def _service_quick_actions(service, *labels: str) -> list[object]:
    actions: list[object] = []
    link_action = _service_link_action(service)
    if link_action is not None:
        actions.append(link_action)
    actions.extend(labels)
    return actions


def _contact_safe_context(
    message: str,
    phone: str,
    service,
    known_services: list[object] | None = None,
    *,
    booking_request: bool = False,
    service_unresolved: bool = False,
) -> dict[str, object]:
    safe_context: dict[str, object] = {
        "contact": {
            "name": extract_name(message, phone, known_services=known_services),
            "phone": phone,
        },
        "service": service.model_dump() if service else None,
    }
    if booking_request:
        safe_context["booking_request"] = True
    if service_unresolved:
        safe_context["service_unresolved"] = True
        safe_context["unresolved_query"] = message.strip()
    return safe_context


def _article_quick_actions(matches: list[dict[str, object]]) -> list[object]:
    actions: list[object] = []
    top_url = str(matches[0].get("url") or "").strip() if matches else ""
    if top_url:
        actions.append({"label": "Читать статью", "type": "link", "value": top_url})
    actions.append("Позвать менеджера")
    return actions


def _retrieve_article_context_safe(message: str) -> list[dict[str, object]]:
    try:
        return retrieve_article_context(message)
    except FileNotFoundError:
        logger.warning("rag article corpus not found; faq_question will clarify")
        return []
    except ValueError as error:
        logger.warning("rag article corpus invalid; faq_question will clarify error=%s", type(error).__name__)
        return []


def _article_guidance_quick_actions(services: list[object]) -> list[object]:
    actions = [
        {"label": service.name, "type": "message", "value": service.name}
        for service in services
    ]
    actions.append("Позвать менеджера")
    return actions


def _article_guidance_result_from_entry(
    knowledge_base: KnowledgeBase,
    entry,
    *,
    match: dict[str, object] | None = None,
    matched_phrase: str = "",
) -> PolicyResult | None:
    services = [
        knowledge_base.find_service_by_id(service_id)
        for service_id in getattr(entry, "service_ids", [])
    ]
    services = [service for service in services if service is not None]
    if not services:
        return None

    service_names = ", ".join(service.name for service in services)
    caution = str(getattr(entry, "extra_caution_note", "") or "").strip()
    if not caution:
        caution = (
            "Заочно нельзя определить, что подойдёт именно вам — "
            "это уточнит специалист на консультации."
        )
    message_to_user = (
        f"По теме «{entry.title}» у нас обычно рассматривают: {service_names}. "
        f"{caution}"
    )
    excerpt = str(getattr(entry, "excerpt", "") or "").strip()
    article_guidance_candidate = None
    if excerpt:
        article_guidance_candidate = {
            "title": entry.title,
            "url": entry.url,
            "excerpt": excerpt,
            "service_names": service_names,
            "caution": caution,
            "fallback_message_to_user": message_to_user,
        }
    article_context = [match] if match else [
        {
            "title": entry.title,
            "url": entry.url,
            "snippet": excerpt,
            "chunk_id": None,
            "score": None,
            "source": "trigger_phrase",
            "matched_phrase": matched_phrase,
        }
    ]

    result = PolicyResult(
        action=PolicyAction.ANSWER,
        reason=PolicyReason.OK,
        # Однозначная услуга — запоминаем как контекст (как similar_services_result), чтобы
        # follow-up («а сколько это стоит?») резолвился. Несколько кандидатов — намеренно НЕ
        # угадываем один: session context вместо этого хранит весь список кандидатов, см.
        # _context_frame_from_policy_result → "cosmetic_candidates".
        service_id=services[0].id if len(services) == 1 else None,
        confidence=0.8,
        safe_context={
            "force_direct_answer": True,
            "message_to_user": message_to_user,
            "question_type": "cosmetic_article_guidance",
            "article_context": article_context,
            "article_service_mapping": {
                "title": entry.title,
                "url": entry.url,
                "service_ids": list(entry.service_ids),
                "trigger_phrases": list(getattr(entry, "trigger_phrases", [])),
                "matched_phrase": matched_phrase,
                "extra_caution_note": caution,
                "excerpt_present": bool(excerpt),
            },
            "suggested_services": services_summary(services),
        },
        quick_actions=_article_guidance_quick_actions(services),
    )
    if article_guidance_candidate is not None:
        result.safe_context["article_guidance_candidate"] = article_guidance_candidate
    return result


def _cosmetic_article_guidance_result(
    knowledge_base: KnowledgeBase,
    article_matches: list[dict[str, object]],
    normalized_message: str = "",
) -> PolicyResult | None:
    approved_map = getattr(knowledge_base, "article_service_map", {}) or {}
    if not approved_map:
        return None

    if normalized_message:
        for entry in approved_map.values():
            for phrase in getattr(entry, "trigger_phrases", []):
                normalized_phrase = normalize_text(str(phrase))
                if normalized_phrase and contains_keyword(normalized_message, {normalized_phrase}):
                    result = _article_guidance_result_from_entry(
                        knowledge_base,
                        entry,
                        matched_phrase=normalized_phrase,
                    )
                    if result is not None:
                        return result

    for match in article_matches:
        url = str(match.get("url") or "").strip().rstrip("/")
        if not url:
            continue
        entry = approved_map.get(url)
        if entry is None:
            continue

        result = _article_guidance_result_from_entry(knowledge_base, entry, match=match)
        if result is not None:
            return result

    return None


def _has_strong_article_overlap(normalized_message: str, guidance_result: PolicyResult) -> bool:
    """Отсекает случайные однословные RAG-совпадения (например "руки" в статье про
    уход после 30, где это просто одна из зон тела, а не то, о чём спросил пользователь) —
    для мягкого off-topic редиректа нужно 2+ значимых пересечения, не одно случайное слово.
    Куратированный trigger_phrase-матч (человек уже подтвердил фразу) считается достаточным
    сам по себе."""

    mapping = guidance_result.safe_context.get("article_service_mapping")
    if isinstance(mapping, dict) and str(mapping.get("matched_phrase") or "").strip():
        return True

    article_context = guidance_result.safe_context.get("article_context")
    if not isinstance(article_context, list) or not article_context:
        return False

    message_tokens = {token for token in normalized_message.split() if len(token) >= 4}
    for item in article_context:
        if not isinstance(item, dict):
            continue
        text = f"{item.get('title') or ''} {item.get('snippet') or ''}"
        text_tokens = {token for token in normalize_text(text).split() if len(token) >= 4}
        if len(message_tokens & text_tokens) >= 2:
            return True
    return False


def _service_variant_examples(service, limit: int = 5) -> list[str]:
    variants = getattr(service, "variants", [])
    if not isinstance(variants, list):
        return []

    examples: list[str] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        name = str(variant.get("name") or "").strip()
        price_text = str(variant.get("price_text") or "").strip()
        if not name:
            continue
        examples.append(f"{name} — {price_text}" if price_text else name)
        if len(examples) >= limit:
            break
    return examples


def _service_explanation_message(service) -> str:
    examples = _service_variant_examples(service)
    if examples:
        variants_count = len(getattr(service, "variants", []) or [])
        return (
            f"{service.name} — направление с {variants_count} вариантами в прайсе. "
            "Например: "
            + "; ".join(examples)
            + ". Точный вариант и стоимость лучше подтвердить с менеджером."
        )
    return f"{service.name} — {service.short_description} Детали уточнит менеджер."


def _clinic_info(knowledge_base: KnowledgeBase) -> dict[str, object]:
    config = getattr(knowledge_base, "config_payload", {})
    clinic_info = config.get("clinic_info") if isinstance(config, dict) else None
    return clinic_info if isinstance(clinic_info, dict) else {}


def _clinic_facts(knowledge_base: KnowledgeBase) -> dict[str, object]:
    facts = _clinic_info(knowledge_base).get("facts")
    return facts if isinstance(facts, dict) else {}


def _clinic_doctors(knowledge_base: KnowledgeBase) -> list[dict[str, str]]:
    raw_doctors = _clinic_info(knowledge_base).get("doctors")
    if not isinstance(raw_doctors, list):
        return []

    doctors: list[dict[str, str]] = []
    for item in raw_doctors:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        specialty = str(item.get("specialty") or "").strip()
        schedule = str(item.get("schedule") or "").strip()
        if name:
            doctors.append({"name": name, "specialty": specialty, "schedule": schedule})
    return doctors


def _clinic_equipment(knowledge_base: KnowledgeBase) -> list[dict[str, object]]:
    raw_equipment = _clinic_info(knowledge_base).get("equipment")
    if not isinstance(raw_equipment, list):
        return []

    equipment: list[dict[str, object]] = []
    for item in raw_equipment:
        if not isinstance(item, dict):
            continue
        service_id = str(item.get("service_id") or "").strip()
        aliases = [
            str(alias).strip()
            for alias in item.get("question_aliases", [])
            if str(alias).strip()
        ]
        equipment.append(
            {
                "service_id": service_id or None,
                "question_aliases": aliases,
                "equipment_name": str(item.get("equipment_name") or "").strip() or None,
                "public_answer": str(item.get("public_answer") or "").strip(),
                "disclose": bool(item.get("disclose") is True),
            }
        )
    return equipment


def undisclosed_equipment_terms(knowledge_base: KnowledgeBase) -> list[str]:
    """Бренд-специфичные термины оборудования, которые LLM не должен подтверждать в ответе —
    последняя линия защиты валидатора, независимая от захардкоженного общего списка в
    validator.py. Только записи с реальным equipment_name (не null) — для записей, где
    equipment_name не задан, question_aliases это синонимы НАЗВАНИЯ УСЛУГИ ("лазерная
    эпиляция"/"эпиляция"), а не бренда, их блокировать нельзя.

    Даже когда equipment_name задан, question_aliases для этой же записи могут СМЕШИВАТЬ
    бренд-токены ("морфеус", "инмод") с синонимами самой услуги ("рф лифтинг", "игольчатый
    rf") — на реальных данных ROSH это подтвердилось: те же 3 фразы дословно совпадают с
    services.json.synonyms связанной услуги. Такие фразы нужны в обычных ответах постоянно —
    блокировать их нельзя. Оставляем только алиасы, которых нет среди имени/синонимов услуги."""

    terms: list[str] = []
    for equipment in _clinic_equipment(knowledge_base):
        if equipment.get("disclose") is True:
            continue
        equipment_name = equipment.get("equipment_name")
        if not equipment_name:
            continue
        terms.append(str(equipment_name))

        service = knowledge_base.find_service_by_id(str(equipment.get("service_id") or ""))
        service_terms = {str(service.name).strip().lower()} | {
            str(synonym).strip().lower() for synonym in (service.synonyms if service else [])
        }
        for alias in equipment.get("question_aliases") or []:
            if str(alias).strip().lower() not in service_terms:
                terms.append(str(alias))
    return terms


def _should_suppress_service_variant_examples(
    knowledge_base: KnowledgeBase,
    service,
) -> bool:
    if service is None:
        return False
    for equipment in _clinic_equipment(knowledge_base):
        if equipment.get("service_id") == service.id and equipment.get("disclose") is not True:
            return True
    return False


def _service_mention_context(knowledge_base: KnowledgeBase, service) -> dict[str, object]:
    context = knowledge_base.get_service_context(service)
    if not _should_suppress_service_variant_examples(knowledge_base, service):
        return context

    service_payload = context.get("service")
    if isinstance(service_payload, dict):
        context["service"] = {
            **service_payload,
            "suppress_variant_examples": True,
        }
    return context


_BOOKING_TARGET_SKIP_WORDS = {
    "завтра",
    "сегодня",
    "послезавтра",
    "утро",
    "утром",
    "день",
    "днем",
    "вечер",
    "вечером",
    "ночь",
    "ночью",
    "понедельник",
    "вторник",
    "среду",
    "четверг",
    "пятницу",
    "субботу",
    "воскресенье",
    "выходные",
    "неделе",
    "следующей",
}


def _has_unknown_booking_target(normalized_message: str) -> bool:
    tokens = normalized_message.split()
    for index, token in enumerate(tokens[:-1]):
        if token != "на":
            continue
        next_token = tokens[index + 1]
        if next_token in _BOOKING_TARGET_SKIP_WORDS:
            continue
        if next_token.isdigit():
            continue
        return True
    return False


def _looks_like_bare_price_question(normalized_message: str) -> bool:
    tokens = normalized_message.split()
    if "сколько" not in tokens:
        return False
    if tokens.index("сколько") >= len(tokens) - 1:
        return False
    if contains_keyword(normalized_message, DURATION_KEYWORDS):
        return False
    if contains_keyword(normalized_message, EXPLANATION_KEYWORDS):
        return False
    return True


def _clinic_sensitive_topics(knowledge_base: KnowledgeBase) -> list[dict[str, object]]:
    raw_topics = _clinic_info(knowledge_base).get("sensitive_topics")
    if not isinstance(raw_topics, list):
        return []

    topics: list[dict[str, object]] = []
    for item in raw_topics:
        if not isinstance(item, dict):
            continue
        keywords = [
            normalize_text(str(keyword))
            for keyword in item.get("keywords", [])
            if normalize_text(str(keyword))
        ]
        if not keywords:
            continue
        handling = str(item.get("handling") or "escalate").strip().lower()
        if handling not in {"escalate", "decline"}:
            handling = "escalate"
        topics.append(
            {
                "keywords": keywords,
                "handling": handling,
                "text": str(item.get("text") or "").strip(),
                "offer_lead": item.get("offer_lead") is not False,
            }
        )
    return topics


def _sensitive_topic_match(normalized_message: str, knowledge_base: KnowledgeBase) -> dict[str, object] | None:
    for topic in _clinic_sensitive_topics(knowledge_base):
        keywords = topic.get("keywords") if isinstance(topic.get("keywords"), list) else []
        if any(keyword in normalized_message for keyword in keywords):
            return topic

    if contains_keyword(normalized_message, DEFAULT_SENSITIVE_TOPIC_KEYWORDS):
        return {
            "keywords": [],
            "handling": "escalate",
            "text": "",
            "offer_lead": True,
        }
    return None


def _consultation_service_for_referral(normalized_message: str, knowledge_base: KnowledgeBase):
    candidates = [
        service
        for service in knowledge_base.services
        if "консультац" in normalize_text(
            " ".join([service.name, service.category, *service.synonyms])
        )
    ]
    if not candidates:
        return None

    if contains_keyword(normalized_message, {"родинк", "гистолог", "новообраз", "дерматоскоп"}):
        for service in candidates:
            service_text = normalize_text(" ".join([service.name, service.category, *service.synonyms]))
            if any(keyword in service_text for keyword in ("дермат", "косметолог", "врач")):
                return service
    return candidates[0]


def _medical_referral_quick_actions(consultation_service, *, offer_lead: bool = True) -> list[object]:
    actions: list[object] = []
    if consultation_service is not None:
        actions.append(
            {
                "label": consultation_service.name,
                "type": "message",
                "value": consultation_service.name,
            }
        )
    if offer_lead:
        actions.append("Оставить телефон")
    actions.append("Позвать менеджера")
    return actions


OBJECTION_PHRASE_KEYS = {
    "price": "objection_price",
    "hesitation": "objection_hesitation",
    "competitor": "objection_competitor",
    "guarantee": "objection_guarantee",
}
OBJECTION_BACKOFF_ATTEMPTS = 2
OBJECTION_QUICK_ACTIONS = ["Позвать менеджера", "Посмотреть услуги"]


def _objection_result(
    knowledge_base: KnowledgeBase,
    session: Session,
    classification: dict[str, object],
    classifier_confidence: float,
) -> Optional[PolicyResult]:
    """Возражения (цена/"подумаю"/конкуренты/гарантии) — детерминированные шаблоны, не
    LLM-генерация: локальная модель ненадёжно следует инструкциям "не критиковать
    конкурентов"/"не обещать результат", а часть формулировок несёт юридический вес.
    После двух мягких попыток бот не настаивает третий раз (см. tasks/Скрипты..., п.4.6)."""

    objection_topic = str(classification.get("context_topic") or "")
    phrase_key = OBJECTION_PHRASE_KEYS.get(objection_topic)
    if phrase_key is None:
        return None

    response_count = int(session.objection_response_count or 0)
    if response_count >= OBJECTION_BACKOFF_ATTEMPTS:
        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.OBJECTION_BACKOFF,
            confidence=classifier_confidence or 0.9,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": _phrase(
                    knowledge_base, "objection_backoff", seed=_phrase_seed(session, "objection_backoff")
                ),
                "objection_topic": objection_topic,
            },
            quick_actions=OBJECTION_QUICK_ACTIONS,
        )

    return PolicyResult(
        action=PolicyAction.ANSWER,
        reason=PolicyReason.OBJECTION_HANDLED,
        confidence=classifier_confidence or 0.9,
        safe_context={
            "force_direct_answer": True,
            "message_to_user": _phrase(knowledge_base, phrase_key, seed=_phrase_seed(session, phrase_key)),
            "objection_topic": objection_topic,
        },
        quick_actions=OBJECTION_QUICK_ACTIONS,
    )


def _medical_referral_result(
    normalized_message: str,
    knowledge_base: KnowledgeBase,
    session: Session,
    service,
    restricted_category: str | None,
) -> PolicyResult:
    consultation_service = None
    if contains_keyword(normalized_message, MEDICAL_REFERRAL_KEYWORDS):
        consultation_service = _consultation_service_for_referral(normalized_message, knowledge_base)

    message_to_user = (
        _phrase(knowledge_base, "medical_referral", seed=_phrase_seed(session, "medical_referral"))
        or knowledge_base.company.safety_disclaimer
    )
    # "а больно?"/"это нормально?" — бытовые вопросы, попавшие в MEDICAL_KEYWORDS вместе с
    # реально острыми сигналами (кровотечение, аллергия). Soft-offer текст (regulated_soft_offer)
    # для ВСЕХ них одинаково заканчивался "если срочно — звоните... в скорую (103)" — пугает на
    # безобидном вопросе. is_benign_only=True только если В СООБЩЕНИИ НЕТ других медицинских
    # слов (безопасный дефолт на смешанных фразах вроде "болит и кровит").
    is_benign_only = contains_keyword(normalized_message, BENIGN_MEDICAL_KEYWORDS) and not contains_keyword(
        normalized_message, MEDICAL_KEYWORDS - BENIGN_MEDICAL_KEYWORDS
    )
    return PolicyResult(
        action=PolicyAction.TRANSFER_OPERATOR,
        reason=PolicyReason.REGULATED_ADVICE,
        service_id=service.id if service else None,
        confidence=0.98,
        safe_context={
            "force_direct_answer": True,
            "message_to_user": message_to_user,
            "handoff_message": message_to_user,
            "restricted_category": restricted_category,
            "referral_service": consultation_service.model_dump() if consultation_service else None,
            "escalation_urgency": "calm" if is_benign_only else "urgent",
        },
        quick_actions=_medical_referral_quick_actions(consultation_service),
    )


def _sensitive_topic_result(
    topic: dict[str, object],
    normalized_message: str,
    knowledge_base: KnowledgeBase,
    session: Session,
    service,
    restricted_category: str | None,
) -> PolicyResult:
    handling = str(topic.get("handling") or "escalate")
    configured_text = str(topic.get("text") or "").strip()
    offer_lead = topic.get("offer_lead") is not False
    consultation_service = _consultation_service_for_referral(normalized_message, knowledge_base)

    if handling == "decline":
        message_to_user = configured_text or _phrase(
            knowledge_base,
            "sensitive_decline",
            seed=_phrase_seed(session, "sensitive_decline"),
        )
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.REGULATED_ADVICE,
            service_id=service.id if service else None,
            confidence=0.98,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": message_to_user,
                "restricted_category": restricted_category,
                "sensitive_handling": "decline",
            },
            quick_actions=["Посмотреть услуги", "Позвать менеджера"],
        )

    message_to_user = configured_text or _phrase(
        knowledge_base,
        "sensitive_escalate",
        seed=_phrase_seed(session, "sensitive_escalate"),
    )
    return PolicyResult(
        action=PolicyAction.TRANSFER_OPERATOR,
        reason=PolicyReason.REGULATED_ADVICE,
        service_id=service.id if service else None,
        confidence=0.98,
        safe_context={
            "force_direct_answer": True,
            "message_to_user": message_to_user,
            "handoff_message": message_to_user,
            "restricted_category": restricted_category,
            "sensitive_handling": "escalate",
            "referral_service": consultation_service.model_dump() if consultation_service else None,
        },
        quick_actions=_medical_referral_quick_actions(consultation_service, offer_lead=offer_lead),
    )


def _equipment_matches(message: str, service, equipment: dict[str, object]) -> bool:
    service_id = equipment.get("service_id")
    if service is not None and service_id and service.id == service_id:
        return True

    normalized_message = normalize_text(message)
    aliases = equipment.get("question_aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            normalized_alias = normalize_text(str(alias))
            if normalized_alias and normalized_alias in normalized_message:
                return True
    return False


def _equipment_result(
    message: str,
    normalized_message: str,
    knowledge_base: KnowledgeBase,
    session: Session,
    service,
) -> PolicyResult | None:
    if not contains_keyword(normalized_message, EQUIPMENT_QUESTION_KEYWORDS):
        return None

    matched_equipment = None
    for equipment in _clinic_equipment(knowledge_base):
        if _equipment_matches(message, service, equipment):
            matched_equipment = equipment
            break

    if matched_equipment is not None:
        service_id = str(matched_equipment.get("service_id") or "") or None
        equipment_name = str(matched_equipment.get("equipment_name") or "").strip()
        public_answer = str(matched_equipment.get("public_answer") or "").strip()
        disclose = matched_equipment.get("disclose") is True
        if disclose and equipment_name:
            message_to_user = public_answer or f"Используется аппарат: {equipment_name}."
            action = PolicyAction.ANSWER
        else:
            message_to_user = public_answer or _phrase(
                knowledge_base,
                "equipment_deferred",
                seed=_phrase_seed(session, "equipment_deferred"),
            )
            action = PolicyAction.CLARIFY
        return PolicyResult(
            action=action,
            reason=PolicyReason.OK,
            service_id=service_id,
            confidence=0.92,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": message_to_user,
                "equipment": matched_equipment,
            },
            quick_actions=["Оставить телефон", "Позвать менеджера"],
        )

    return PolicyResult(
        action=PolicyAction.CLARIFY,
        reason=PolicyReason.OK,
        confidence=0.86,
        safe_context={
            "force_direct_answer": True,
            "message_to_user": _phrase(
                knowledge_base,
                "equipment_deferred",
                seed=_phrase_seed(session, "equipment_deferred"),
            ),
        },
        quick_actions=["Оставить телефон", "Позвать менеджера"],
    )


def _doctor_matches(message: str, doctor: dict[str, str]) -> bool:
    normalized_message = normalize_text(message)

    # NER сначала: находит упоминание человека в СЫРОМ сообщении (регистр важен), а не гоняет
    # префиксное сравнение по КАЖДОМУ слову сообщения вслепую — тот же класс риска ложного
    # совпадения, что уже давал баг на услугах ("биорезонансная" ⊃ "биоревитализация" по
    # префиксу). Сравниваем леммы по префиксу токена, не на точное равенство — pymorphy2 не
    # всегда согласован сам с собой между падежами одной фамилии.
    message_lemma = extract_person_lemma_via_ner(message)
    if message_lemma:
        doctor_lemma = lemmatize_known_name(doctor.get("name", "")) or ""
        message_lemma_tokens = [token for token in message_lemma.split() if len(token) >= 3]
        doctor_lemma_tokens = [token for token in doctor_lemma.split() if len(token) >= 3]
        if any(
            _token_prefix_match(doctor_token, msg_token)
            for doctor_token in doctor_lemma_tokens
            for msg_token in message_lemma_tokens
        ):
            return True

    # Фолбэк — как раньше: NER не нашёл сущность (natasha не загрузилась, опечатка сломала
    # распознавание и т.д.), не теряем совпадение вслепую.
    message_tokens = [token for token in normalized_message.split() if len(token) >= 3]
    name_tokens = [token for token in normalize_text(doctor.get("name", "")).split() if len(token) >= 3]
    if any(
        _token_prefix_match(name_token, msg_token)
        for name_token in name_tokens
        for msg_token in message_tokens
    ):
        return True
    specialty = normalize_text(doctor.get("specialty", ""))
    return bool(specialty and specialty in normalized_message)


def _format_doctors(doctors: list[dict[str, str]]) -> str:
    values = []
    for doctor in doctors:
        specialty = doctor.get("specialty", "")
        values.append(f"{doctor['name']} — {specialty}" if specialty else doctor["name"])
    return "; ".join(values)


def _format_doctor_schedules(doctors: list[dict[str, str]]) -> str:
    values = []
    for doctor in doctors:
        schedule = doctor.get("schedule", "")
        if not schedule:
            continue
        values.append(f"{doctor['name']}: {schedule}")
    return "; ".join(values)


def _looks_like_placeholder_address(address: str) -> bool:
    normalized = normalize_text(address)
    return not normalized or "уточняется" in normalized or "уточнит" in normalized


URGENT_SYMPTOM_KEYWORDS = {
    "кров",
    "болит",
    "больно",
    "гной",
    "температура",
    "тошнит",
    "головокруж",
    "немеет",
    "онем",
    "отек",
    "отёк",
    "аллерг",
    "зуд",
    "жжение",
    "воспален",
}


def _is_ambulance_fact_question(normalized_message: str) -> bool:
    return contains_keyword(normalized_message, AMBULANCE_SUBJECT_KEYWORDS) and contains_keyword(
        normalized_message,
        AMBULANCE_ACTION_KEYWORDS,
    )


def _has_urgent_symptom(normalized_message: str) -> bool:
    return contains_keyword(normalized_message, URGENT_SYMPTOM_KEYWORDS)


def _clinic_info_result(
    message: str,
    normalized_message: str,
    knowledge_base: KnowledgeBase,
    session: Session,
    context_topic: str | None = None,
    booking_requested: bool = False,
) -> PolicyResult | None:
    company = knowledge_base.company
    doctors = _clinic_doctors(knowledge_base)
    matched_doctors = [doctor for doctor in doctors if _doctor_matches(message, doctor)]
    doctor_name_matched = bool(matched_doctors)
    tokens = normalized_message.split()
    facts = _clinic_facts(knowledge_base)
    base_quick_actions = ["Оставить телефон", "Позвать менеджера"]

    if contains_keyword(normalized_message, CLINIC_LOCATION_KEYWORDS):
        address = str(company.address or "").strip()
        if _looks_like_placeholder_address(address):
            message_to_user = _format_phrase(
                knowledge_base,
                "clinic_location_deferred",
                company_name=company.company_name,
                city=company.city,
                working_hours=company.working_hours,
            )
        else:
            message_to_user = _format_phrase(
                knowledge_base,
                "clinic_location",
                company_name=company.company_name,
                city=company.city,
                address=address,
                working_hours=company.working_hours,
            )
        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.OK,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": message_to_user,
                "clinic_info_topic": "location",
            },
            quick_actions=base_quick_actions,
        )

    doctor_schedule_requested = contains_keyword(normalized_message, DOCTOR_SCHEDULE_KEYWORDS)
    if contains_keyword(normalized_message, {"когда работает"}) and not doctor_name_matched:
        doctor_schedule_requested = False
    if doctor_name_matched and "когда" in tokens:
        doctor_schedule_requested = True
    if doctor_schedule_requested:
        schedule_text = _format_doctor_schedules(matched_doctors)
        if schedule_text:
            message_to_user = _format_phrase(
                knowledge_base,
                "doctor_schedule_from_data",
                schedule=schedule_text,
            )
            return PolicyResult(
                action=PolicyAction.ANSWER,
                reason=PolicyReason.OK,
                confidence=0.9,
                safe_context={
                    "force_direct_answer": True,
                    "message_to_user": message_to_user,
                    "clinic_info_topic": "doctors",
                },
                quick_actions=base_quick_actions,
            )
        doctor_note = f" По врачу: {_format_doctors(matched_doctors)}." if matched_doctors else ""
        message_to_user = _format_phrase(
            knowledge_base,
            "doctor_schedule_deferred",
            working_hours=company.working_hours,
        ) + doctor_note
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.OK,
            confidence=0.88,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": message_to_user,
                "clinic_info_topic": "doctors",
            },
            quick_actions=base_quick_actions,
        )

    doctor_info_requested = (
        context_topic == "doctors"
        or contains_keyword(normalized_message, DOCTOR_INFO_KEYWORDS)
        or (doctor_name_matched and not booking_requested)
    )
    if doctor_info_requested:
        asked_generic_list = contains_keyword(normalized_message, GENERIC_DOCTOR_LIST_KEYWORDS)
        if matched_doctors:
            selected_doctors = matched_doctors
        elif asked_generic_list:
            selected_doctors = doctors
        else:
            selected_doctors = []
        if selected_doctors:
            message_to_user = _format_phrase(
                knowledge_base,
                "doctors_from_data",
                doctors=_format_doctors(selected_doctors[:5]),
            )
            action = PolicyAction.ANSWER
        else:
            message_to_user = _phrase(knowledge_base, "doctors_deferred")
            action = PolicyAction.CLARIFY
        return PolicyResult(
            action=action,
            reason=PolicyReason.OK,
            confidence=0.88,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": message_to_user,
                "clinic_info_topic": "doctors",
            },
            quick_actions=base_quick_actions,
        )

    fact_key = ""
    fact_value = None
    if contains_keyword(normalized_message, OMS_FACT_KEYWORDS):
        fact_value = facts.get("oms")
        fact_key = "fact_oms_yes" if fact_value is True else "fact_oms_no" if fact_value is False else ""
    elif contains_keyword(normalized_message, DMS_FACT_KEYWORDS):
        fact_value = facts.get("dms")
        fact_key = "fact_dms_yes" if fact_value is True else "fact_dms_no" if fact_value is False else ""
    elif contains_keyword(normalized_message, AMBULANCE_SUBJECT_KEYWORDS) and contains_keyword(
        normalized_message, AMBULANCE_ACTION_KEYWORDS
    ):
        fact_value = facts.get("ambulance_brings")
        fact_key = (
            "fact_ambulance_yes"
            if fact_value is True
            else "fact_ambulance_no"
            if fact_value is False
            else ""
        )
    elif contains_keyword(normalized_message, PRODUCTS_FACT_KEYWORDS):
        fact_value = facts.get("sells_products")
        fact_key = (
            "fact_products_yes"
            if fact_value is True
            else "fact_products_no"
            if fact_value is False
            else ""
        )

    if fact_key or fact_value is None and (
        contains_keyword(normalized_message, OMS_FACT_KEYWORDS)
        or contains_keyword(normalized_message, DMS_FACT_KEYWORDS)
        or (
            contains_keyword(normalized_message, AMBULANCE_SUBJECT_KEYWORDS)
            and contains_keyword(normalized_message, AMBULANCE_ACTION_KEYWORDS)
        )
        or contains_keyword(normalized_message, PRODUCTS_FACT_KEYWORDS)
    ):
        message_to_user = (
            _phrase(knowledge_base, fact_key)
            if fact_key
            else _phrase(
                knowledge_base,
                "clinic_fact_deferred",
                seed=_phrase_seed(session, "clinic_fact_deferred"),
            )
        )
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.OK,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": message_to_user,
                "clinic_info_topic": "facts",
            },
            quick_actions=base_quick_actions,
        )

    return None


def _efficacy_claim_result(normalized_message: str, knowledge_base: KnowledgeBase, service) -> PolicyResult | None:
    if not contains_keyword(normalized_message, EFFICACY_CLAIM_KEYWORDS):
        return None

    consultation_service = _consultation_service_for_referral(normalized_message, knowledge_base)
    message_to_user = _phrase(knowledge_base, "efficacy_claim_deferred") or _phrase(
        knowledge_base,
        "medical_referral",
    )
    return PolicyResult(
        action=PolicyAction.CLARIFY,
        reason=PolicyReason.REGULATED_ADVICE,
        service_id=service.id if service else None,
        confidence=0.9,
        safe_context={
            "force_direct_answer": True,
            "message_to_user": message_to_user,
            "referral_service": consultation_service.model_dump() if consultation_service else None,
            "claim_deferred": True,
        },
        quick_actions=_medical_referral_quick_actions(consultation_service),
    )


def _history_indicates_lead_sent(session: Session) -> bool:
    for item in session.messages[-6:]:
        if str(item.role) not in {"MessageRole.ASSISTANT", "assistant"}:
            continue
        normalized_text = normalize_text(item.text)
        if any(marker in normalized_text for marker in ("заявку передали", "контакты передали", "зафиксировал")):
            return True
    return False


def _lead_followup_result(normalized_message: str, knowledge_base: KnowledgeBase) -> PolicyResult | None:
    if "?" in normalized_message:
        return None
    tokens = normalized_message.split()
    if not tokens or len(tokens) > 3:
        return None
    if not contains_keyword(normalized_message, LEAD_FOLLOWUP_SHORT_KEYWORDS):
        return None
    return PolicyResult(
        action=PolicyAction.CLARIFY,
        reason=PolicyReason.CONTACT_PROVIDED,
        confidence=0.86,
        safe_context={
            "force_direct_answer": True,
            "message_to_user": _phrase(knowledge_base, "lead_followup"),
        },
        quick_actions=["Позвать менеджера", "Посмотреть услуги"],
    )


def _has_wide_price_range(service) -> bool:
    variants = getattr(service, "variants", []) or []
    if not variants:
        return False
    price_from = getattr(service, "price_from", None)
    price_to = getattr(service, "price_to", None)
    if not isinstance(price_from, (int, float)) or not isinstance(price_to, (int, float)):
        return False
    if price_from <= 0 or price_to <= 0:
        return False
    return price_to / price_from >= WIDE_PRICE_RANGE_RATIO


def _wide_price_range_clarify_result(
    knowledge_base: KnowledgeBase,
    service,
    context: dict[str, object],
    *,
    confidence: float,
) -> PolicyResult | None:
    if not _has_wide_price_range(service):
        return None
    labels = variant_list_labels(service, limit=8)
    if not labels:
        return None
    variants = getattr(service, "variants", []) or []
    remaining = max(0, len(variants) - len(labels))
    tail = f" и ещё {remaining}" if remaining else ""
    price_disclaimer = _phrase(
        knowledge_base,
        "price_disclaimer",
        "Это предварительная стоимость. Точную сумму подтвердит менеджер после уточнения деталей.",
    )
    message_to_user = (
        f"У услуги «{service.name}» цена сильно зависит от варианта: {', '.join(labels)}{tail}. "
        "Уточните, какой вариант интересует, и я подскажу цену по конкретной позиции. "
        f"{price_disclaimer}"
    )
    return PolicyResult(
        action=PolicyAction.CLARIFY,
        reason=PolicyReason.PRICE_QUESTION,
        service_id=service.id,
        confidence=confidence,
        safe_context={
            **context,
            "force_direct_answer": True,
            "question_type": "variants_list",
            "message_to_user": message_to_user,
        },
        quick_actions=_service_quick_actions(service, "Оставить телефон"),
    )


def _variant_price_answer(
    knowledge_base: KnowledgeBase,
    service,
    matches: list[dict[str, object]],
    *,
    confidence: float,
) -> PolicyResult:
    lines = [variant_price_line(service, variant) for variant in matches]
    price_disclaimer = _phrase(
        knowledge_base,
        "price_disclaimer",
        "Это предварительная стоимость. Точную сумму подтвердит менеджер после уточнения деталей.",
    )
    message_to_user = (
        f"По услуге «{service.name}»: {'; '.join(line for line in lines if line)}. "
        f"{price_disclaimer}"
    )
    return PolicyResult(
        action=PolicyAction.ANSWER,
        reason=PolicyReason.PRICE_QUESTION,
        service_id=service.id,
        confidence=confidence,
        safe_context={
            **knowledge_base.get_service_context(service),
            "force_direct_answer": True,
            "question_type": "variant_price",
            "message_to_user": message_to_user,
            "variant_matches": matches,
        },
        quick_actions=_service_quick_actions(service, "Оставить телефон"),
    )


def _known_service_price_result(
    knowledge_base: KnowledgeBase,
    service,
    message: str = "",
    *,
    confidence: float = 0.95,
) -> PolicyResult:
    context = knowledge_base.get_service_context(service)
    if context.get("price") is None:
        return PolicyResult(
            action=PolicyAction.ASK_CONTACT,
            reason=PolicyReason.PRICE_QUESTION,
            service_id=service.id,
            confidence=confidence,
            safe_context={
                **context,
                "message_to_user": _phrase(knowledge_base, "contact_prompt"),
            },
        )

    if message:
        # Пользователь мог сразу назвать конкретный вариант ("Механическая чистка лица
        # цена") — тогда отвечаем по нему напрямую, а не общим списком всех вариантов.
        variant_matches = find_variant_matches(service, message)
        if variant_matches:
            return _variant_price_answer(
                knowledge_base,
                service,
                variant_matches,
                confidence=confidence,
            )

    wide_range_result = _wide_price_range_clarify_result(
        knowledge_base,
        service,
        context,
        confidence=confidence,
    )
    if wide_range_result is not None:
        return wide_range_result

    return PolicyResult(
        action=PolicyAction.ANSWER,
        reason=PolicyReason.PRICE_QUESTION,
        service_id=service.id,
        confidence=confidence,
        safe_context={**context, "question_type": "price"},
        quick_actions=_service_quick_actions(service, "Оставить телефон"),
    )


def _variant_followup_result(
    message: str,
    knowledge_base: KnowledgeBase,
    service,
    context_topic: str,
    context_variant: dict[str, object] | None = None,
) -> PolicyResult | None:
    variants = getattr(service, "variants", []) or []
    if not variants:
        return None

    if context_topic == "variant_repeat_price" and context_variant is not None:
        matches = [context_variant]
    else:
        matches = find_variant_matches(service, message)

    # Конкретное совпадение варианта важнее общего списка: слова вроде "зона"/"варианты"
    # входят и в общий вопрос ("какие зоны?"), и в названия самих вариантов ("Т зона"),
    # поэтому список показываем только если точный вариант не нашёлся.
    if not matches and (context_topic == "variants_list" or is_variant_list_question(message)):
        labels = variant_list_labels(service, limit=8)
        if not labels:
            return None
        remaining = max(0, len(variants) - len(labels))
        tail = f" и ещё {remaining}" if remaining else ""
        message_to_user = (
            f"По услуге «{service.name}» есть варианты: {', '.join(labels)}{tail}. "
            "Могу подсказать цену по конкретной зоне или позиции."
        )
        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.OK,
            service_id=service.id,
            confidence=0.9,
            safe_context={
                **knowledge_base.get_service_context(service),
                "force_direct_answer": True,
                "question_type": "variants_list",
                "message_to_user": message_to_user,
            },
            quick_actions=_service_quick_actions(service, "Уточнить цену", "Оставить телефон"),
        )

    if not matches:
        return None

    return _variant_price_answer(knowledge_base, service, matches, confidence=0.92)


FACT_VALUE_QUESTION_KEYWORDS = {
    "какие препараты",
    "какой препарат",
    "какие материалы",
    "какой материал",
    "какие филлеры",
    "какой филлер",
    "что используете",
    "чем работаете",
}
HARD_RESTRICTED_KEYWORDS = {
    "диагноз",
    "лечить",
    "назначить",
    "назначьте",
    "выпишите",
    "таблет",
    "чем мазать",
    "чем помазать",
    "помазать",
    "намазать",
    "какой крем от",
    "какую мазь",
    "какая мазь",
    "мазь от",
    "крем от аллергии",
    "крем от раздражения",
    "крем от ожога",
    "антибиотик",
    "воспаление",
    "воспален",
    "кровит",
    "кровоточ",
    "родинка",
    "родинк",
    "гистолог",
    "аборт",
    "кокков",
    "бактер",
    "опасно",
    "аллергия",
    "зуд",
    "раздражение",
    "беремен",
    "покраснение",
    "припухлость",
    "отек",
    "отёк",
    "инфекция",
    "осложнение",
    "болит",
    "боли",
    "больно",
    "болью",
    "температура",
    "симптом",
    "жжение",
    "жжет",
    "немеет",
    "немеют",
    "онемение",
    "онемел",
    "тошнит",
    "тошнота",
    "головокружение",
    "кружи",
    "гной",
    "гное",
    "опух",
    "щиплет",
    "щипет",
    "прокапат",
    "прокапаю",
    "капельниц",
    "капельницу",
    "прокапаться",
    "назначени",
    "назначил",
    "без консультации",
    "без осмотра",
    "другой врач",
    "другого врача",
    "рецепт",
    "по назначению",
}
FACT_GUARD_NEGATION_MARKERS = {
    "не интересно",
    "не интересует",
    "не нужен",
    "не нужно",
    "не хочу",
    "не надо",
}
FACT_GUARD_EDUCATIONAL_MARKERS = {
    "зачем",
    "для чего",
    "что такое",
    "как действует",
    "как работает",
    "из чего состоит",
    "в чем разница",
    "в чём разница",
}
FACT_GUARD_AVAILABILITY_MARKERS = {
    "у вас",
    "вы делаете",
    "вы колете",
    "здесь есть",
    "в клинике",
    "можно у вас",
    "делаете ли",
    "колете ли",
    "есть ли у вас",
}
SAFE_SERVICE_REQUEST_INTENTS = {
    "medical_advice",
    "regulated_advice",
    "price_question",
    "service_mention",
}


def _has_hard_restricted_signal(normalized_message: str) -> bool:
    return contains_keyword(normalized_message, HARD_RESTRICTED_KEYWORDS | MEDICAL_KEYWORDS)


def _looks_like_safe_known_service_request(intent: str, normalized_message: str, service) -> bool:
    if service is None or intent not in SAFE_SERVICE_REQUEST_INTENTS:
        return False
    return not _has_hard_restricted_signal(normalized_message)


def _has_fact_guard_negation(normalized_message: str) -> bool:
    return contains_keyword(normalized_message, FACT_GUARD_NEGATION_MARKERS)


def _fact_guard_segments(normalized_message: str) -> list[str]:
    return [
        segment.strip()
        for segment in re.split(r"\b(?:но|а|зато|однако)\b", normalized_message)
        if segment.strip()
    ]


def _fact_guard_value_is_negated(normalized_message: str, normalized_value: str) -> bool:
    if not normalized_value:
        return False
    segments = _fact_guard_segments(normalized_message)
    for index, segment in enumerate(segments):
        if normalized_value not in segment:
            continue
        if _has_fact_guard_negation(segment):
            return True
        if index + 1 < len(segments) and _has_fact_guard_negation(segments[index + 1]):
            return True
        return False
    return _has_fact_guard_negation(normalized_message)


def _fact_guard_is_educational_question(normalized_message: str) -> bool:
    """Вопросы вида "зачем/для чего делают X" — это не попытка узнать, есть ли у нас
    конкретный незаявленный бренд, а общеобразовательный вопрос. Guard не должен их блокировать,
    только реальные "у вас есть/делаете ли вы X"."""

    if not contains_keyword(normalized_message, FACT_GUARD_EDUCATIONAL_MARKERS):
        return False
    return not contains_keyword(normalized_message, FACT_GUARD_AVAILABILITY_MARKERS)


def _fact_guard_result(message: str, knowledge_base: KnowledgeBase) -> PolicyResult | None:
    config = getattr(knowledge_base, "config_payload", {})
    fact_guards = config.get("fact_guards") if isinstance(config, dict) else None
    if not isinstance(fact_guards, list):
        return None

    normalized_message = normalize_text(message)
    for guard in fact_guards:
        if not isinstance(guard, dict):
            continue

        topic = str(guard.get("topic") or "").strip()
        service_id = str(guard.get("service_id") or "").strip() or None
        known_values = [
            str(value).strip()
            for value in guard.get("known_values", [])
            if str(value).strip()
        ]
        blocked_values = [
            str(value).strip()
            for value in guard.get("blocked_values", [])
            if str(value).strip()
        ]
        matched_blocked = [
            value
            for value in blocked_values
            if normalize_text(value) and normalize_text(value) in normalized_message
        ]
        matched_blocked = [
            value
            for value in matched_blocked
            if not _fact_guard_value_is_negated(normalized_message, normalize_text(value))
        ]
        if not matched_blocked:
            continue
        if _fact_guard_is_educational_question(normalized_message):
            continue

        service = knowledge_base.find_service_by_id(service_id)
        allowed_text = ", ".join(known_values) if known_values else "только позиции из базы центра"
        blocked_text = ", ".join(matched_blocked)
        message_to_user = str(guard.get("message_to_user") or "").strip()
        if not message_to_user:
            topic_label = topic or (service.name if service else "услуга")
            message_to_user = (
                f"По теме «{topic_label}» доступны: {allowed_text}. "
                f"{blocked_text} среди подтверждённых вариантов нет. "
                "Могу показать страницу услуги или передать вопрос менеджеру."
            )
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.UNKNOWN_SERVICE,
            service_id=service.id if service else service_id,
            confidence=0.96,
            safe_context={
                "message_to_user": message_to_user,
                "service": service.model_dump() if service else None,
                "fact_guard": {
                    "topic": topic,
                    "matched_blocked": matched_blocked,
                    "known_values": known_values,
                },
            },
            quick_actions=_service_quick_actions(service, "Позвать менеджера", "Посмотреть услуги")
            if service
            else ["Позвать менеджера", "Посмотреть услуги"],
        )

    return None


def _fact_guard_known_values_result(
    message: str,
    knowledge_base: KnowledgeBase,
    service,
) -> PolicyResult | None:
    if not contains_keyword(normalize_text(message), FACT_VALUE_QUESTION_KEYWORDS):
        return None

    config = getattr(knowledge_base, "config_payload", {})
    fact_guards = config.get("fact_guards") if isinstance(config, dict) else None
    if not isinstance(fact_guards, list):
        return None

    normalized_message = normalize_text(message)
    for guard in fact_guards:
        if not isinstance(guard, dict):
            continue

        topic = str(guard.get("topic") or "").strip()
        service_id = str(guard.get("service_id") or "").strip() or None
        guard_service = knowledge_base.find_service_by_id(service_id)
        known_values = [
            str(value).strip()
            for value in guard.get("known_values", [])
            if str(value).strip()
        ]
        topic_matches = bool(topic and normalize_text(topic) in normalized_message)
        service_matches = bool(service is not None and service_id and service.id == service_id)
        guard_service_matches = bool(
            guard_service is not None
            and (
                normalize_text(guard_service.name) in normalized_message
                or any(
                    normalized_synonym and normalized_synonym in normalized_message
                    for normalized_synonym in (normalize_text(synonym) for synonym in guard_service.synonyms)
                )
            )
        )
        if not (topic_matches or service_matches or guard_service_matches):
            continue

        selected_service = guard_service or service
        if known_values:
            topic_label = topic or (selected_service.name if selected_service else "этой теме")
            message_to_user = f"По теме «{topic_label}» указаны: {', '.join(known_values)}."
        else:
            topic_label = topic or (selected_service.name if selected_service else "этой теме")
            message_to_user = (
                f"Точный список по теме «{topic_label}» не указан. "
                "Лучше уточнить у менеджера."
            )

        return PolicyResult(
            action=PolicyAction.ANSWER if known_values else PolicyAction.CLARIFY,
            reason=PolicyReason.OK if known_values else PolicyReason.UNKNOWN_SERVICE,
            service_id=selected_service.id if selected_service else service_id,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": message_to_user,
                "service": selected_service.model_dump() if selected_service else None,
                "fact_guard": {
                    "topic": topic,
                    "known_values": known_values,
                },
            },
            quick_actions=_service_quick_actions(selected_service, "Уточнить цену", "Позвать менеджера")
            if selected_service
            else ["Позвать менеджера", "Посмотреть услуги"],
        )

    return None


def analyze_message(
    message: str,
    session: Session,
    knowledge_base: KnowledgeBase,
    classification: Optional[dict[str, object]] = None,
) -> PolicyResult:
    """классифицирует сообщение до любого взаимодействия с llm."""

    classification = normalize_classification(classification or {})
    intent = str(classification["intent"])
    classifier_confidence = float(classification["confidence"])
    normalized_message = normalize_text(message)
    service = knowledge_base.find_service_by_id(classification.get("service_id"))
    if intent == "price_question" and normalized_message in GENERIC_PRICE_MESSAGES:
        service = None
    if service is None and (
        intent == "price_question"
        or contains_keyword(normalized_message, DURATION_KEYWORDS)
        or contains_keyword(normalized_message, EXPLANATION_KEYWORDS)
    ):
        service = knowledge_base.find_service_by_id(
            session.last_service_id or last_service_from_history(session, knowledge_base)
        )
    if intent == "bot_identity":
        # Детерминированная честная ветка — не даём "живой человек" внутри вопроса "ты бот
        # или живой человек?" провалиться в operator_requested ниже по коду (та проверка
        # матчит OPERATOR_REQUEST_KEYWORDS по сырому сообщению независимо от intent).
        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.OK,
            confidence=classifier_confidence or 0.95,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": _phrase(knowledge_base, "bot_identity_confirm"),
            },
            quick_actions=["Посмотреть услуги", "Позвать менеджера"],
        )

    phone = extract_phone(message)
    operator_requested = contains_keyword(
        normalized_message, set(knowledge_base.company.operator_triggers)
    ) or contains_keyword(normalized_message, OPERATOR_REQUEST_KEYWORDS) or intent == "operator_request"
    duration_requested = contains_keyword(normalized_message, DURATION_KEYWORDS)
    explanation_requested = contains_keyword(normalized_message, EXPLANATION_KEYWORDS)
    price_requested = (
        intent == "price_question"
        or fuzzy_contains(normalized_message, PRICE_KEYWORDS)
        or _looks_like_bare_price_question(normalized_message)
    )
    booking_requested = intent == "booking_request" or contains_keyword(normalized_message, BOOKING_KEYWORDS)
    lead_requested = intent == "lead_request" or contains_keyword(normalized_message, LEAD_REQUEST_KEYWORDS)
    booking_mentions_clinic_doctor = booking_requested and any(
        _doctor_matches(message, doctor) for doctor in _clinic_doctors(knowledge_base)
    )
    is_restricted, restricted_category = is_restricted_question(message, knowledge_base.domain_profile)
    medical_requested = intent in {"medical_advice", "regulated_advice"} or is_restricted
    if medical_requested and _looks_like_safe_known_service_request(intent, normalized_message, service):
        medical_requested = False
    unsupported_city = find_unsupported_city(normalized_message, knowledge_base.company.city, message=message)
    city_in_text = city_prepositional(knowledge_base.company.city)
    sensitive_topic = _sensitive_topic_match(normalized_message, knowledge_base)

    if _is_ambulance_fact_question(normalized_message) and not _has_urgent_symptom(normalized_message):
        clinic_info_result = _clinic_info_result(
            message,
            normalized_message,
            knowledge_base,
            session,
            str(classification.get("context_topic") or "") or None,
            booking_requested=booking_requested,
        )
        if clinic_info_result is not None:
            return clinic_info_result

    if sensitive_topic is not None:
        return _sensitive_topic_result(
            sensitive_topic,
            normalized_message,
            knowledge_base,
            session,
            service,
            restricted_category,
        )

    if medical_requested:
        if not _has_hard_restricted_signal(normalized_message):
            article_matches = _retrieve_article_context_safe(message)
            guidance_result = _cosmetic_article_guidance_result(
                knowledge_base,
                article_matches,
                normalized_message,
            )
            if guidance_result is not None:
                return guidance_result
        return _medical_referral_result(
            normalized_message,
            knowledge_base,
            session,
            service,
            restricted_category,
        )

    if session.pending_action == PendingAction.COLLECT_CONTACT.value and contains_keyword(
        normalized_message, NEGATIVE_MESSAGES
    ):
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.CONTACT_PROVIDED,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "contact_request_cancelled": True,
                "message_to_user": _phrase(knowledge_base, "contact_cancelled"),
            },
            quick_actions=["Посмотреть услуги", "Позвать менеджера"],
        )

    if session.pending_action == PendingAction.BOOKING_CONTACT.value and contains_keyword(
        normalized_message, NEGATIVE_MESSAGES
    ):
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.BOOKING_REQUEST,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "booking_request_cancelled": True,
                "message_to_user": _phrase(knowledge_base, "booking_cancelled"),
            },
            quick_actions=["Посмотреть услуги", "Позвать менеджера"],
        )

    if contains_keyword(normalized_message, NEGATIVE_MESSAGES):
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.OK,
            confidence=0.86,
            safe_context={
                "force_direct_answer": True,
                "general_cancelled": True,
                "message_to_user": _phrase(knowledge_base, "general_cancelled"),
            },
            quick_actions=["Посмотреть услуги", "Позвать менеджера"],
        )

    if phone and session.lead_requested:
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.CONTACT_PROVIDED,
            service_id=service.id if service else None,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": (
                    "Контакты уже передали менеджеру. Если нужно изменить заявку, "
                    "допишите детали здесь или позовите менеджера."
                ),
            },
            quick_actions=["Позвать менеджера", "Посмотреть услуги"],
        )

    if phone and lead_requested:
        service_unresolved = service is None and intent == "unknown_service"
        return PolicyResult(
            action=PolicyAction.ASK_CONTACT,
            reason=PolicyReason.CONTACT_PROVIDED,
            service_id=service.id if service else None,
            confidence=0.94,
            safe_context=_contact_safe_context(
                message,
                phone,
                service,
                knowledge_base.services,
                service_unresolved=service_unresolved,
            ),
        )

    looks_like_new_question = "?" in message or classifier_confidence > 0
    if (
        session.pending_action == PendingAction.BOOKING_CONTACT.value
        and not phone
        and not operator_requested
        and not looks_like_new_question
    ):
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.BOOKING_REQUEST,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "booking_request": True,
                "message_to_user": _phrase(knowledge_base, "booking_contact_prompt"),
            },
            quick_actions=["Оставить телефон", "Позвать менеджера"],
        )

    if intent == "location_mismatch" or is_location_mismatch(
        message, normalized_message, knowledge_base.company.city
    ):
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.LOCATION_MISMATCH,
            confidence=classifier_confidence or 0.86,
            safe_context={
                "company_city": knowledge_base.company.city,
                "message_to_user": (
                    f"Очный приём только в {city_in_text}. "
                    "Уточните, пожалуйста, у менеджера — возможно есть удалённый формат консультации для вашего случая."
                ),
                "context_for_model": {
                    "company_city": knowledge_base.company.city,
                    "note": (
                        f"очный приём только в {knowledge_base.company.city}, "
                        "можно уточнить формат у менеджера"
                    ),
                },
            },
            quick_actions=[
                {
                    "label": "Позвать менеджера",
                    "type": "message",
                    "value": "Хочу узнать про удалённый формат",
                }
            ],
        )

    if intent == "objection":
        objection_result = _objection_result(knowledge_base, session, classification, classifier_confidence)
        if objection_result is not None:
            return objection_result

    clinic_info_result = _clinic_info_result(
        message,
        normalized_message,
        knowledge_base,
        session,
        str(classification.get("context_topic") or "") or None,
        booking_requested=booking_requested,
    )
    if clinic_info_result is not None:
        return clinic_info_result

    if session.lead_requested or _history_indicates_lead_sent(session):
        lead_followup_result = _lead_followup_result(normalized_message, knowledge_base)
        if lead_followup_result is not None:
            return lead_followup_result
        if (
            service is not None
            and intent == "service_mention"
            and not booking_requested
            and not price_requested
            and "?" not in message
        ):
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.CONTACT_PROVIDED,
                service_id=service.id,
                confidence=0.85,
                safe_context={
                    "force_direct_answer": True,
                    "message_to_user": _format_phrase(
                        knowledge_base,
                        "lead_followup_service",
                        service_name=service.name,
                    ),
                },
                quick_actions=["Позвать менеджера", "Посмотреть услуги"],
            )

    if intent == "small_talk":
        return PolicyResult(
            action=PolicyAction.SMALL_TALK,
            reason=PolicyReason.SMALL_TALK,
            confidence=classifier_confidence or 0.9,
            safe_context={
                "company_name": knowledge_base.company.company_name,
                "phrasebook": getattr(knowledge_base, "phrasebook", {}),
            },
        )

    if intent == "off_topic":
        if contains_keyword(normalized_message, BODY_TOPIC_SIGNAL_KEYWORDS):
            article_matches = _retrieve_article_context_safe(message)
            guidance_result = _cosmetic_article_guidance_result(
                knowledge_base,
                article_matches,
                normalized_message,
            )
            if guidance_result is not None and _has_strong_article_overlap(
                normalized_message, guidance_result
            ):
                return guidance_result

            # Не называем случайную услугу наугад при слабом совпадении (см. "руки" —
            # одно случайное слово раньше давало уверенный, но неверный матч) — вместо
            # этого честный мягкий редирект. Не повторяем питч второй раз подряд в сессии.
            already_offered = session.last_intent == PolicyReason.OFF_TOPIC_BODY_REDIRECT.value
            phrase_key = (
                "off_topic_body_redirect_repeat" if already_offered else "off_topic_body_redirect"
            )
            return PolicyResult(
                action=PolicyAction.OFF_TOPIC,
                reason=PolicyReason.OFF_TOPIC_BODY_REDIRECT,
                confidence=classifier_confidence or 0.85,
                safe_context={
                    "force_direct_answer": True,
                    "message_to_user": _phrase(
                        knowledge_base,
                        phrase_key,
                        seed=_phrase_seed(session, "off_topic_body_redirect"),
                    ),
                },
                quick_actions=["Позвать менеджера", "Посмотреть услуги"],
            )

        return PolicyResult(
            action=PolicyAction.OFF_TOPIC,
            reason=PolicyReason.OFF_TOPIC,
            confidence=classifier_confidence or 0.9,
            safe_context={
                "message_to_user": _phrase(
                    knowledge_base,
                    "off_topic",
                    seed=_phrase_seed(session, "off_topic"),
                )
                or (
                    "Это не по моей части — я консультирую по услугам компании. "
                    f"{knowledge_base.company.company_name}. Могу подсказать по услугам или ценам."
                )
            },
            quick_actions=["Посмотреть услуги", "Позвать менеджера"],
        )

    fact_guard_result = _fact_guard_result(message, knowledge_base)
    if fact_guard_result is not None:
        return fact_guard_result

    fact_guard_known_values_result = _fact_guard_known_values_result(message, knowledge_base, service)
    if fact_guard_known_values_result is not None:
        return fact_guard_known_values_result

    if intent == "clarify":
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.OK,
            confidence=classifier_confidence or 0.7,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": _phrase(knowledge_base, "clarify")
                or "Не совсем понял. Уточните, пожалуйста, услугу, цену или вопрос для менеджера.",
            },
            quick_actions=["Посмотреть услуги", "Позвать менеджера"],
        )

    equipment_result = _equipment_result(message, normalized_message, knowledge_base, session, service)
    if equipment_result is not None:
        return equipment_result

    if intent == "list_services":
        # Куратированная статья (например сезонный уход) может отвечать точнее, чем
        # безусловный полный каталог — но только при уверенном совпадении (curated
        # trigger_phrase или 2+ значимых слова пересечения), иначе для честного "покажи все
        # услуги" каталог остаётся правильным ответом.
        article_matches = _retrieve_article_context_safe(message)
        guidance_result = _cosmetic_article_guidance_result(
            knowledge_base,
            article_matches,
            normalized_message,
        )
        if guidance_result is not None and _has_strong_article_overlap(
            normalized_message, guidance_result
        ):
            return guidance_result

        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.OK,
            confidence=classifier_confidence or 0.9,
            safe_context={
                "all_services": all_services_context(knowledge_base),
                "question_type": "list_services",
            },
            quick_actions=["Уточнить цену", "Позвать менеджера"],
        )

    if intent == "contact_link":
        wants_telegram = contains_keyword(normalized_message, TELEGRAM_KEYWORDS)
        wants_website = contains_keyword(normalized_message, WEBSITE_KEYWORDS)
        wants_visit = contains_keyword(normalized_message, VISIT_KEYWORDS)
        if wants_visit:
            message_to_user = (
                f"Очный приём проходит в {city_in_text}. "
                "Можно уточнить запись и подходящий формат у менеджера."
            )
            quick_actions = ["Позвать менеджера", "Открыть сайт"]
        elif wants_telegram and not wants_website:
            message_to_user = "Можно написать нам в Telegram — кнопка ниже."
            quick_actions = ["Написать в Telegram"]
        elif wants_website and not wants_telegram:
            message_to_user = "Сайт центра можно открыть по кнопке ниже."
            quick_actions = ["Открыть сайт"]
        else:
            message_to_user = "Могу дать ссылку на сайт или Telegram, а при необходимости позвать менеджера."
            quick_actions = ["Написать в Telegram", "Открыть сайт", "Позвать менеджера"]

        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.OK,
            confidence=classifier_confidence or 0.88,
            safe_context={"message_to_user": message_to_user},
            quick_actions=quick_actions,
        )

    if lead_requested:
        return PolicyResult(
            action=PolicyAction.ASK_CONTACT,
            reason=PolicyReason.CONTACT_PROVIDED,
            service_id=service.id if service else None,
            confidence=classifier_confidence or 0.88,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": _phrase(knowledge_base, "contact_prompt"),
                "service": service.model_dump() if service else None,
            },
            quick_actions=["Позвать менеджера", "Посмотреть услуги"],
        )

    if unsupported_city:
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.UNSUPPORTED_CITY,
            service_id=service.id if service else None,
            confidence=0.82,
            safe_context={
                "city_note": (
                    f"Очный приём только в {city_in_text}. "
                    "Можем уточнить формат."
                )
            },
            quick_actions=["Позвать менеджера", "Написать в Telegram"],
        )

    if intent == "cosmetic_concern":
        # Follow-up ("а сколько это стоит?") на СПИСОК из нескольких кандидатов, который мы
        # только что явно предложили (см. chat_utils._contextual_frame_classification,
        # frame_type="cosmetic_candidates") — переспрашиваем среди тех же вариантов, а не
        # угадываем один и не проваливаемся в общий "не нашёл подтверждения".
        context_candidate_ids = classification.get("context_candidate_service_ids")
        if isinstance(context_candidate_ids, list) and context_candidate_ids:
            candidate_services = [
                found
                for service_id in context_candidate_ids
                if (found := knowledge_base.find_service_by_id(str(service_id))) is not None
            ]
            if candidate_services:
                service_names = ", ".join(service.name for service in candidate_services)
                return PolicyResult(
                    action=PolicyAction.CLARIFY,
                    reason=PolicyReason.OK,
                    confidence=classifier_confidence or 0.88,
                    safe_context={
                        "message_to_user": (
                            f"Уточните, пожалуйста, какая процедура интересует: {service_names}? "
                            "Так подскажу точнее."
                        ),
                    },
                    quick_actions=[
                        {"label": service.name, "type": "message", "value": service.name}
                        for service in candidate_services
                    ]
                    + ["Позвать менеджера"],
                )

        # Куратированная статья (человек уже проверил формулировку и подобрал услуги) —
        # более конкретный и информативный ответ, чем общий шаблон ниже. Пробуем её первой;
        # шаблон "обычно подходят: X, Y" — фолбэк для симптомов без готовой статьи.
        article_matches = _retrieve_article_context_safe(message)
        guidance_result = _cosmetic_article_guidance_result(
            knowledge_base,
            article_matches,
            normalized_message,
        )
        if guidance_result is not None:
            return guidance_result

        suggested_services = cosmetic_concern_services(message, knowledge_base)
        if suggested_services:
            service_names = ", ".join(service.name for service in suggested_services)
            return PolicyResult(
                action=PolicyAction.ANSWER,
                reason=PolicyReason.OK,
                confidence=classifier_confidence or 0.82,
                safe_context={
                    "question_type": "cosmetic_concern",
                    "suggested_services": services_summary(suggested_services),
                    "domain_profile": knowledge_base.domain_profile,
                    "message_to_user": (
                        f"Для такого запроса обычно подходят: {service_names}. "
                        "Точные рекомендации даст менеджер на консультации."
                    ),
                },
                quick_actions=[
                    {"label": service.name, "type": "message", "value": service.name}
                    for service in suggested_services
                ]
                + ["Позвать менеджера"],
            )

    efficacy_claim_result = _efficacy_claim_result(normalized_message, knowledge_base, service)
    if efficacy_claim_result is not None:
        return efficacy_claim_result

    if service is not None and not booking_requested:
        variant_result = _variant_followup_result(
            message,
            knowledge_base,
            service,
            str(classification.get("context_topic") or ""),
            classification.get("context_variant") if isinstance(classification.get("context_variant"), dict) else None,
        )
        if variant_result is not None:
            return variant_result

    if intent == "faq_question" and not price_requested and not duration_requested:
        article_query = f"{service.name} {message}" if service is not None else message
        article_matches = _retrieve_article_context_safe(article_query)

        guidance_result = _cosmetic_article_guidance_result(
            knowledge_base,
            article_matches,
            normalized_message,
        )
        if guidance_result is not None:
            return guidance_result

        if not article_matches:
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.FAQ_QUESTION,
                confidence=classifier_confidence or 0.7,
                safe_context={
                    "message_to_user": (
                        "По этому вопросу лучше уточнить у менеджера — подключить?"
                    )
                },
                quick_actions=["Позвать менеджера", "Посмотреть услуги"],
            )

        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.FAQ_QUESTION,
            confidence=classifier_confidence or 0.85,
            safe_context={
                "article_context": article_matches,
                "question_type": "faq_question",
                "domain_profile": knowledge_base.domain_profile,
                "phrasebook": getattr(knowledge_base, "phrasebook", {}),
            },
            quick_actions=_article_quick_actions(article_matches),
        )

    if intent == "unknown_service":
        if phone:
            return PolicyResult(
                action=PolicyAction.ASK_CONTACT,
                reason=PolicyReason.UNKNOWN_SERVICE,
                service_id=None,
                confidence=classifier_confidence or 0.82,
                safe_context=_contact_safe_context(
                    message,
                    phone,
                    None,
                    knowledge_base.services,
                    service_unresolved=True,
                ),
            )

        similar_result = similar_services_result(message, knowledge_base, classifier_confidence or 0.78)
        if similar_result is not None:
            return similar_result

        article_matches = _retrieve_article_context_safe(message)
        guidance_result = _cosmetic_article_guidance_result(
            knowledge_base,
            article_matches,
            normalized_message,
        )
        if guidance_result is not None and _has_strong_article_overlap(
            normalized_message, guidance_result
        ):
            return guidance_result

        if contains_keyword(normalized_message, BODY_TOPIC_SIGNAL_KEYWORDS):
            # Реальный маршрут для тем вроде "узи полового члена" — классификатор чаще
            # даёт unknown_service, а не off_topic, для таких сообщений (проверено живьём).
            # "Похожие варианты" тут не предлагаем — для тем вне профиля клиники их
            # обычно честно нет, лучше прямой мягкий редирект на консультацию.
            already_offered = session.last_intent == PolicyReason.OFF_TOPIC_BODY_REDIRECT.value
            phrase_key = (
                "off_topic_body_redirect_repeat" if already_offered else "off_topic_body_redirect"
            )
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.OFF_TOPIC_BODY_REDIRECT,
                confidence=classifier_confidence or 0.85,
                safe_context={
                    "force_direct_answer": True,
                    "message_to_user": _phrase(
                        knowledge_base,
                        phrase_key,
                        seed=_phrase_seed(session, "off_topic_body_redirect"),
                    ),
                },
                quick_actions=["Позвать менеджера", "Посмотреть услуги"],
            )

        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.UNKNOWN_SERVICE,
            confidence=classifier_confidence or 0.8,
            safe_context={
                "message_to_user": _unknown_service_message(
                    knowledge_base,
                    session,
                    message,
                    normalized_message,
                )
            },
            quick_actions=["Позвать менеджера", "Посмотреть услуги"],
        )

    if operator_requested:
        if phone:
            return PolicyResult(
                action=PolicyAction.ASK_CONTACT,
                reason=PolicyReason.CONTACT_PROVIDED,
                service_id=service.id if service else None,
                confidence=0.95,
                safe_context=_contact_safe_context(message, phone, service, knowledge_base.services),
            )
        # Кнопка "Передать администратору" из engagement-offer напоминания — уже явное
        # подтверждение (пользователь отвечает на прямой вопрос "подключить
        # администратора?"), а не первое двусмысленное упоминание оператора. Без этой
        # проверки такой клик снова уходил в soft-offer вместо реальной передачи.
        operator_already_confirmed = (
            session.pending_action == PendingAction.OFFERED_OPERATOR.value
            or normalized_message == "передать администратору"
        )
        if not operator_already_confirmed:
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.OPERATOR_REQUESTED,
                service_id=service.id if service else None,
                confidence=0.9,
                safe_context={
                    "message_to_user": _phrase(knowledge_base, "operator_soft_offer")
                },
                quick_actions=[
                    {
                        "label": "Сразу к менеджеру",
                        "type": "message",
                        "value": "Да, менеджера",
                    },
                    {
                        "label": "Сначала спрошу тут",
                        "type": "message",
                        "value": "Сначала спрошу тут",
                    },
                ],
            )
        return PolicyResult(
            action=PolicyAction.TRANSFER_OPERATOR,
            reason=PolicyReason.OPERATOR_REQUESTED,
            service_id=service.id if service else None,
            confidence=0.95,
            safe_context={"message_to_user": _phrase(knowledge_base, "handoff_message")},
            quick_actions=["Написать в Telegram", "Открыть сайт"],
        )

    if price_requested and booking_requested and service is not None:
        return _known_service_price_result(knowledge_base, service, message, confidence=0.95)

    if booking_requested:
        if service is None and not booking_mentions_clinic_doctor:
            service = knowledge_base.find_service_by_id(
                session.last_service_id or last_service_from_history(session, knowledge_base)
            )
        if service is None and _has_unknown_booking_target(normalized_message):
            if phone:
                return PolicyResult(
                    action=PolicyAction.ASK_CONTACT,
                    reason=PolicyReason.UNKNOWN_SERVICE,
                    confidence=0.82,
                    safe_context=_contact_safe_context(
                        message,
                        phone,
                        None,
                        knowledge_base.services,
                        booking_request=True,
                        service_unresolved=True,
                    ),
                )
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.UNKNOWN_SERVICE,
                confidence=0.82,
                safe_context={
                    "message_to_user": _unknown_service_message(
                        knowledge_base,
                        session,
                        message,
                        normalized_message,
                    )
                },
                quick_actions=["Посмотреть услуги", "Позвать менеджера"],
            )
        if service is None and not phone:
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.BOOKING_REQUEST,
                confidence=0.86,
                safe_context={
                    "force_direct_answer": True,
                    "booking_request": True,
                    "message_to_user": "На какую услугу хотите оставить заявку?",
                },
                quick_actions=service_name_quick_actions(knowledge_base),
            )
        if phone:
            return PolicyResult(
                action=PolicyAction.ASK_CONTACT,
                reason=PolicyReason.BOOKING_REQUEST,
                service_id=service.id if service else None,
                confidence=0.94,
                safe_context=_contact_safe_context(
                    message,
                    phone,
                    service,
                    knowledge_base.services,
                    booking_request=True,
                ),
            )
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.BOOKING_REQUEST,
            service_id=service.id if service else None,
            confidence=0.9,
            safe_context={
                "force_direct_answer": True,
                "booking_request": True,
                "message_to_user": _phrase(knowledge_base, "booking_contact_prompt"),
            },
            quick_actions=["Оставить телефон", "Позвать менеджера"],
        )

    if phone and session.pending_action == PendingAction.BOOKING_CONTACT.value:
        if service is None:
            service = knowledge_base.find_service_by_id(
                session.last_service_id or last_service_from_history(session, knowledge_base)
            )
        return PolicyResult(
            action=PolicyAction.ASK_CONTACT,
            reason=PolicyReason.BOOKING_REQUEST,
            service_id=service.id if service else None,
            confidence=0.94,
            safe_context=_contact_safe_context(
                message,
                phone,
                service,
                knowledge_base.services,
                booking_request=True,
            ),
        )

    if phone:
        return PolicyResult(
            action=PolicyAction.ASK_CONTACT,
            reason=PolicyReason.CONTACT_PROVIDED,
            service_id=service.id if service else None,
            confidence=0.93,
            safe_context=_contact_safe_context(message, phone, service, knowledge_base.services),
        )

    if price_requested:
        if service is None:
            if not mentions_unknown_service(normalized_message):
                return PolicyResult(
                    action=PolicyAction.CLARIFY,
                    reason=PolicyReason.PRICE_QUESTION_NO_SERVICE,
                    confidence=0.86,
                    safe_context={
                        "message_to_user": "Уточните, пожалуйста, какая услуга вас интересует?",
                        "available_services": [service.name for service in knowledge_base.services],
                    },
                    quick_actions=service_name_quick_actions(knowledge_base),
                )

            similar_result = similar_services_result(message, knowledge_base, 0.78)
            if similar_result is not None:
                return similar_result

            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.UNKNOWN_SERVICE,
                confidence=0.8,
                safe_context={
                    "message_to_user": _unknown_service_message(
                        knowledge_base,
                        session,
                        message,
                        normalized_message,
                    )
                },
                quick_actions=["Позвать менеджера", "Посмотреть услуги"],
            )

        return _known_service_price_result(knowledge_base, service, message)

    if explanation_requested:
        if service is None:
            service = knowledge_base.find_service_by_id(
                session.last_service_id or last_service_from_history(session, knowledge_base)
            )
        if service is None:
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.SERVICE_EXPLANATION,
                confidence=0.78,
                safe_context={
                    "message_to_user": "Уточните, пожалуйста, по какой услуге рассказать подробнее."
                },
                quick_actions=["Посмотреть услуги", "Позвать менеджера"],
            )

        context = knowledge_base.get_service_context(service)
        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.SERVICE_EXPLANATION,
            service_id=service.id,
            confidence=0.9,
            safe_context={
                **context,
                "question_type": "explanation",
                "message_to_user": _service_explanation_message(service),
            },
            quick_actions=_service_quick_actions(service, "Уточнить цену", "Позвать менеджера"),
        )

    if duration_requested:
        if service is None:
            return PolicyResult(
                action=PolicyAction.CLARIFY,
                reason=PolicyReason.UNKNOWN_SERVICE,
                confidence=0.78,
                safe_context={
                    "message_to_user": "Уточните, пожалуйста, по какой услуге нужен срок или длительность."
                },
                quick_actions=["Позвать менеджера", "Посмотреть услуги"],
            )

        context = knowledge_base.get_service_context(service)
        duration = str(getattr(service, "duration", "") or "").strip()
        if not duration:
            return PolicyResult(
                action=PolicyAction.ANSWER,
                reason=PolicyReason.DURATION_QUESTION,
                service_id=service.id,
                confidence=0.9,
                safe_context={
                    **context,
                    "force_direct_answer": True,
                    "question_type": "duration",
                    "message_to_user": (
                        f"Точную длительность по услуге «{service.name}» уточнит менеджер. "
                        "Могу подсказать цену или оформить заявку."
                    ),
                },
                quick_actions=_service_quick_actions(service, "Уточнить цену", "Оставить телефон"),
            )
        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.DURATION_QUESTION,
            service_id=service.id,
            confidence=0.9,
            safe_context={
                **context,
                "force_direct_answer": True,
                "question_type": "duration",
                "message_to_user": (
                    f"По услуге «{service.name}» длительность: {duration}. "
                    "Точное время подтвердит менеджер."
                ),
            },
            quick_actions=_service_quick_actions(service, "Уточнить цену", "Оставить телефон"),
        )

    if service is not None:
        return PolicyResult(
            action=PolicyAction.ANSWER,
            reason=PolicyReason.OK,
            service_id=service.id,
            confidence=0.88,
            safe_context=_service_mention_context(knowledge_base, service),
            quick_actions=_service_quick_actions(service, "Уточнить цену", "Позвать менеджера"),
        )

    if session.status.value != "AI_ACTIVE":
        return PolicyResult(
            action=PolicyAction.REJECT,
            reason=PolicyReason.OUT_OF_SCOPE,
            confidence=0.9,
            safe_context={"message_to_user": "Сейчас чат недоступен для AI-ответов."},
        )

    similar_result = similar_services_result(message, knowledge_base, classifier_confidence or 0.7)
    if similar_result is not None:
        return similar_result

    return PolicyResult(
        action=PolicyAction.CLARIFY,
        reason=PolicyReason.OK,
        confidence=classifier_confidence or 0.65,
        safe_context={
            "message_to_user": _phrase(knowledge_base, "clarify")
            or (
                "Уточните, пожалуйста, что вас интересует? "
                "Могу рассказать про услуги, цены или оформить заявку."
            )
        },
        quick_actions=["Посмотреть услуги", "Позвать менеджера"],
    )
