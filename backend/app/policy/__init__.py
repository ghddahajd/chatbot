"""защитный слой политики, который выполняется до любого вызова llm."""

from __future__ import annotations

import logging
import re
from typing import Optional

from ..knowledge import KnowledgeBase, _token_prefix_match, normalize_text, phrasebook_value_to_text
from ..models import PendingAction, PolicyAction, PolicyReason, PolicyResult, Session
from ..services.rag_search import STOP_WORDS, retrieve_article_context
from .constants import (
    AFFIRMATIVE_MESSAGES,
    BODY_TOPIC_SIGNAL_KEYWORDS,
    BOOKING_KEYWORDS,
    COMPLAINT_ESCALATION_KEYWORDS,
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
    FAQ_QUESTION_KEYWORDS,
    GENERIC_DOCTOR_LIST_KEYWORDS,
    GENERIC_PRICE_MESSAGES,
    HARD_RESTRICTED_KEYWORDS,
    LAB_TEST_KEYWORDS,
    LEAD_REQUEST_KEYWORDS,
    LEAD_FOLLOWUP_SHORT_KEYWORDS,
    ACUTE_ALLERGY_KEYWORDS,
    ACUTE_BLEEDING_KEYWORDS,
    ACUTE_DETERIORATION_KEYWORDS,
    PAIN_INTENSITY_KEYWORDS,
    PAIN_WORDS,
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
    _keyword_token_matches,
    contains_day_or_time_lemma,
    contains_keyword,
    contains_keyword_lemma,
    extract_name,
    extract_person_lemma_via_ner,
    extract_phone,
    find_unsupported_city,
    fuzzy_contains,
    is_location_mismatch,
    last_service_from_history,
    lemmatize_known_name,
    lemmatize_tokens,
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
    "помогите",
    "помощь",
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


def _approved_article_for_service(knowledge_base: KnowledgeBase, service_id: str):
    # Живой баг (2026-08-10): "Мезотерапия" упомянута в service_ids сразу нескольких статей —
    # и как единственная тема ("Мезотерапия кожи головы"), и как один из 3 вариантов в статье
    # про совсем другую жалобу ("Темные круги под глазами", service_ids: [Мезотерапия,
    # Биоревитализация, Филлеры]). Первый найденный по порядку словаря — не обязательно
    # релевантный; предпочитаем статью, где услуга — единственная/одна из немногих тем
    # (меньше service_ids = статья реально ПРО эту услугу, а не мимоходом её упоминает).
    approved_map = getattr(knowledge_base, "article_service_map", {}) or {}
    candidates = [
        entry for entry in approved_map.values() if service_id in getattr(entry, "service_ids", [])
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda entry: len(getattr(entry, "service_ids", []) or []))
    return candidates[0]


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


# Живой баг (аудит §2026-08-22, F-03): "у меня реакция после вашей процедуры помогите"
# пересеклось со статьёй "Процедуры после 30" сразу по 3 токенам ≥4 символов — "после",
# "процедуры", "вашей" (снипет статьи буквально содержит "для вашей кожи... Процедура
# BBL... после 30") — ни один из них не говорит о теме сообщения, это общеупотребимые
# слова, которые почти гарантированно есть в любой статье клиники. Порог "2+" прошёл на
# пустом месте. STOP_WORDS переиспользуем из rag_search (уже курирован для того же класса
# багов — там уже "после"/местоимения); стемы фильтруем отдельно через _token_prefix_match.
#
# Второй живой баг (тот же аудит, persona6): "добрый день, хочу узнать про услуги клиники"
# → статья про капельницы для метаболизма, пересечение по "день"/"клиники" (снипет: "...в
# день процедуры... врачи клиники ROSH рекомендуют..."). Проверил общее правило вместо
# списка (частота слова в корпусе, по аналогии с IDF в rag_search._score_chunk) — не
# подошло эмпирически: в этом узкодоменном корпусе предметные слова вроде "кожи"(56%
# статей)/"лица"(27%)/"врач"(27%) сами частотнее "процедуры"(42%), единый порог либо не
# отсечёт "день"(8%)/"клиники"(5.5%), либо начнёт резать настоящие совпадения. Остаёмся на
# точечных стемах — тут это безопаснее общего правила, а не проще для экономии времени.
#
# "день" короче 6 символов — _token_prefix_match для таких токенов сравнивает только
# точное равенство (не префикс), поэтому его словоформы перечисляем явно, а не через стем.
#
# Третий и четвёртый живой баг (тот же аудит, из таблицы "4 независимых случая", разобраны
# на 2026-08-23): "образование на коже... стесняюсь" → статья "Кожа после лазерной
# шлифовки" по "образование"+"таком" ("В таком случае..." — указательное местоимение,
# та же категория, что личные местоимения в STOP_WORDS, просто другая словоформенная
# семья, раньше не добавляли); "убрать образование в зоне бикини" → "Лазерная эпиляция
# бикини" по "зоне"+"бикини" ("...в зоне глубокого бикини..." — "бикини" тут настоящее
# совпадение по анатомической зоне, а не мусорное слово, но "зона"/"зоне" — служебный
# классификатор участка тела, есть почти в любой zone-specific статье, частота в корпусе
# ~11-12%, тот же класс, что "процедура"/"клиника"). "образование" — не трогаем: это
# настоящая смысловая коллизия (нарост на коже vs "образование корочек"), не generic-слово,
# исключать её нельзя — но без "таком" одного "образование" уже недостаточно для порога "2+".
_WEAK_OVERLAP_STEMS = ("процедура", "клиника")
_WEAK_OVERLAP_EXACT_WORDS = {
    "день", "дней", "дни", "дню", "днём", "дням", "днями", "днях",
    "такой", "такая", "такое", "такие", "таком", "такую", "такими", "таких", "такого", "такому",
    "зона", "зоне", "зоны", "зону", "зоной", "зонах", "зонами",
}


def _significant_overlap_tokens(text: str) -> set[str]:
    return {
        token
        for token in normalize_text(text).split()
        if len(token) >= 4
        and token not in STOP_WORDS
        and token not in _WEAK_OVERLAP_EXACT_WORDS
        and not any(_token_prefix_match(token, stem) for stem in _WEAK_OVERLAP_STEMS)
    }


def _has_strong_article_overlap(normalized_message: str, guidance_result: PolicyResult) -> bool:
    """Отсекает случайные RAG-совпадения по общеупотребимым словам (например "руки" в
    статье про уход после 30, где это просто одна из зон тела, а не то, о чём спросил
    пользователь) — для мягкого off-topic редиректа нужно 2+ значимых пересечения, не
    одно/два случайных общих слова. Куратированный trigger_phrase-матч (человек уже
    подтвердил фразу) считается достаточным сам по себе."""

    mapping = guidance_result.safe_context.get("article_service_mapping")
    if isinstance(mapping, dict) and str(mapping.get("matched_phrase") or "").strip():
        return True

    article_context = guidance_result.safe_context.get("article_context")
    if not isinstance(article_context, list) or not article_context:
        return False

    message_tokens = _significant_overlap_tokens(normalized_message)
    for item in article_context:
        if not isinstance(item, dict):
            continue
        text = f"{item.get('title') or ''} {item.get('snippet') or ''}"
        text_tokens = _significant_overlap_tokens(text)
        if len(message_tokens & text_tokens) >= 2:
            return True
    return False


def _is_first_substantive_message(session: Session) -> bool:
    """До этого сообщения бот в сессии ещё ничего не отвечал — это буквально первая реплика
    диалога. Нужно для §3.2 скрипта: на симптом с первого сообщения — сначала уточняющий
    вопрос, не сразу услуга."""

    for item in session.messages:
        if str(item.role) in {"MessageRole.ASSISTANT", "assistant"}:
            return False
    return True


def _curated_match_is_explicit_service_mention(
    guidance_result: PolicyResult, knowledge_base: KnowledgeBase
) -> bool:
    """Отличает §3.3 скрипта (человек сам назвал услугу/метод — отвечаем прямо, уточняющий
    вопрос уже ПОСЛЕ факта) от §3.2 (человек описал симптом — сначала уточняющий вопрос,
    потом предложение). Сигнал: совпавшая trigger_phrase — это само название услуги/синоним,
    а не смысловое описание проблемы ("выпадают волосы" ни у одной услуги не встречается как
    синоним, "родинки радиоволновым методом" вполне может быть)."""

    mapping = guidance_result.safe_context.get("article_service_mapping")
    if not isinstance(mapping, dict):
        return True
    matched_phrase = normalize_text(str(mapping.get("matched_phrase") or ""))
    if not matched_phrase:
        # score-based (не curated) матч — эта проверка специально про curated trigger_phrase
        # (§3.2 воронка), не про score-based совпадения в принципе. У score-based совпадений
        # часто прямой вопрос про конкретную косметическую проблему ("второй подбородок можно
        # убрать?"), не размытый симптом — гейтить их тем же правилом было бы перебором,
        # ломает уже рабочий, проверенный сценарий.
        return True
    for service_id in mapping.get("service_ids") or []:
        service = knowledge_base.find_service_by_id(service_id)
        if service is None:
            continue
        for term in (service.name, *service.synonyms):
            if matched_phrase == normalize_text(term):
                return True
    return False


def _symptom_followup_result(
    guidance_result: PolicyResult, knowledge_base: KnowledgeBase, session: Session
) -> PolicyResult:
    """Уточняющий вопрос вместо немедленного предложения услуги (§3.2 скрипта). service_id
    (когда услуга одна) прокидывается в safe_context под отдельным маркером —
    _context_frame_from_policy_result в chat_service.py читает его и заводит ОТДЕЛЬНЫЙ,
    изолированный ContextFrame ('symptom_followup', живёт РОВНО 1 шаг), чтобы неопределённый
    ответ на следующем сообщении ("где-то полгода") не проваливался в дефолтный "не нашёл", а
    другие типы фреймов/веток при этом не трогаются и не расширяются."""

    mapping = guidance_result.safe_context.get("article_service_mapping")
    service_ids = mapping.get("service_ids") if isinstance(mapping, dict) else None
    pending_service_id = (
        service_ids[0] if isinstance(service_ids, list) and len(service_ids) == 1 else None
    )
    safe_context: dict[str, object] = {
        "force_direct_answer": True,
        "message_to_user": _phrase(
            knowledge_base,
            "symptom_followup_question",
            seed=_phrase_seed(session, "symptom_followup_question"),
        ),
    }
    if pending_service_id:
        safe_context["symptom_followup_service_id"] = pending_service_id
    return PolicyResult(
        action=PolicyAction.CLARIFY,
        reason=PolicyReason.OK,
        confidence=0.82,
        safe_context=safe_context,
        quick_actions=["Позвать менеджера"],
    )


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


_DOCTOR_SPECIALTY_QUESTION_TEMPLATES = ("как зовут {specialty}", "кто {specialty}", "кто у вас {specialty}")


def _doctor_specialty_info_keywords(doctors: list[dict[str, str]]) -> set[str]:
    """Строит фразы вида 'кто гинеколог'/'как зовут дерматолог' из specialty САМОГО тенанта,
    а не из захардкоженного списка 5 специальностей — раньше это был декартово произведение
    (специальность × формулировка), вписанное вручную и не покрывавшее ничьи специальности,
    кроме тех, что кто-то успел добавить (живой пробел: 'невролог'/'трихолог' у ROSH не
    ловились вообще, хотя есть в данных клиники)."""

    specialties = {normalize_text(doctor.get("specialty", "")) for doctor in doctors}
    specialties.discard("")
    return {
        template.format(specialty=specialty)
        for specialty in specialties
        for template in _DOCTOR_SPECIALTY_QUESTION_TEMPLATES
    }


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
    # Живой баг (2026-08-19): "через сколько можно забеременеть после родов" — "через сколько"
    # спрашивает про срок/время ("how soon"), не про цену, но содержит токен "сколько" не
    # последним словом и не задет DURATION_KEYWORDS (там только "сколько длится"/"по времени" и
    # т.п., не сама конструкция "через сколько"). Ложно взводило price_requested, из-за чего
    # faq_question-ветка (там, где это сообщение реально должно отвечаться) пропускалась целиком.
    if "через сколько" in normalized_message:
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


def _growth_removal_service_for_referral(normalized_message: str, knowledge_base: KnowledgeBase):
    # 2026-08-18: клиент подтвердил, что тема новообразований (родинки/папилломы/бородавки)
    # не настолько деликатная, чтобы прятать саму услугу удаления — но передачу оператору
    # на "родинк"/"новообраз" сохраняем как есть (см. MEDICAL_REFERRAL_KEYWORDS): в отличие
    # от папиллом/бородавок (см. COSMETIC_CONCERN_SERVICE_MAP), тут может стоять вопрос
    # онкологической настороженности, решать заочно не должен ни бот, ни прямая продажа услуги.
    # Поэтому вместо ЗАМЕНЫ хэндофа — дополняем его видимой опцией услуги.
    if not contains_keyword(normalized_message, {"родинк", "новообраз"}):
        return None
    for service in knowledge_base.services:
        service_text = normalize_text(" ".join([service.name, service.category, *service.synonyms]))
        if "новообраз" in service_text:
            return service
    return None


def _medical_referral_quick_actions(
    consultation_service, *, offer_lead: bool = True, extra_service=None
) -> list[object]:
    actions: list[object] = []
    if consultation_service is not None:
        actions.append(
            {
                "label": consultation_service.name,
                "type": "message",
                "value": consultation_service.name,
            }
        )
    if extra_service is not None and (
        consultation_service is None or extra_service.id != consultation_service.id
    ):
        actions.append(
            {
                "label": extra_service.name,
                "type": "message",
                "value": extra_service.name,
            }
        )
    if offer_lead:
        actions.append("Оставить телефон")
    actions.append("Позвать менеджера")
    return actions


def _lab_test_result(
    knowledge_base: KnowledgeBase, session: Session, classifier_confidence: float
) -> PolicyResult:
    return PolicyResult(
        action=PolicyAction.ANSWER,
        reason=PolicyReason.OK,
        confidence=classifier_confidence or 0.85,
        safe_context={
            "force_direct_answer": True,
            "message_to_user": _phrase(
                knowledge_base, "lab_tests_available", seed=_phrase_seed(session, "lab_tests_available")
            ),
        },
        quick_actions=["Позвать менеджера", "Посмотреть услуги"],
    )


# Denylist, не allowlist — см. комментарий у has_competing_substantive_signal. Только
# заведомо несодержательные интенты: чистая болтовня, офтоп, и "классификатор сам не понял"
# (clarify) — единственный случай, когда "нет" в сообщении разумно считать голым отказом
# даже при confidence > 0.
NEGATIVE_CATCHALL_EXCLUDED_INTENTS = {"small_talk", "off_topic", "clarify"}

OBJECTION_PHRASE_KEYS = {
    "price": "objection_price",
    "hesitation": "objection_hesitation",
    "competitor": "objection_competitor",
    "guarantee": "objection_guarantee",
    "pain_fear": "objection_pain_fear",
}
# Живой репро (аудит §2026-08-22): "price" и "hesitation" на практике одна и та же "не готов
# сейчас" мысль, просто разными словами ("дорого, надо подумать") — трейс живого диалога
# (P3_2 → price, P3_4/P3_5 → hesitation, P3_8 «...надо решить — всё дорого» → снова price)
# показал 3 мягких попытки подряд без backoff, потому что классификатор давал им разные
# context_topic на каждой перефразировке, а per-topic счётчик (1a0db39) считает раздельно.
# competitor/guarantee/pain_fear НЕ трогаем — это реально разные тревоги пациента, не варианты
# одной и той же, объединять их было бы неправильно.
OBJECTION_COUNTING_TOPIC = {
    "price": "price_or_hesitation",
    "hesitation": "price_or_hesitation",
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

    # counting_topic ТОЛЬКО для счётчика попыток — objection_topic (специфичный) остаётся
    # как есть для выбора фразы и для проверки ниже (booking_hesitation_hold должен сработать
    # именно на "hesitation", не на объединённом ключе).
    counting_topic = OBJECTION_COUNTING_TOPIC.get(objection_topic, objection_topic)
    response_count = session.objection_response_counts.get(counting_topic, 0)
    # §3.5 скрипта: "думаю"/"посоветуюсь" ИМЕННО на этапе записи (pending_action ==
    # BOOKING_CONTACT) — не тот же вопрос "что смущает" (objection_hesitation), который
    # звучит странно, когда контакт уже запрашивается, а мягкое "придержу интерес без
    # обязательств". Тот же backoff после 2 попыток, что и у остальных возражений.
    if objection_topic == "hesitation" and session.pending_action == PendingAction.BOOKING_CONTACT.value:
        phrase_key = "booking_hesitation_hold"
        quick_actions: list[object] = ["Оставить телефон", "Позвать менеджера"]
    else:
        quick_actions = OBJECTION_QUICK_ACTIONS

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
                "objection_counting_topic": counting_topic,
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
            "objection_counting_topic": counting_topic,
        },
        quick_actions=quick_actions,
    )


def _medical_referral_result(
    message: str,
    normalized_message: str,
    knowledge_base: KnowledgeBase,
    session: Session,
    service,
    restricted_category: str | None,
    phone: str | None = None,
) -> PolicyResult:
    consultation_service = None
    growth_removal_service = None
    if contains_keyword(normalized_message, MEDICAL_REFERRAL_KEYWORDS):
        consultation_service = _consultation_service_for_referral(normalized_message, knowledge_base)
        growth_removal_service = _growth_removal_service_for_referral(normalized_message, knowledge_base)

    message_to_user = (
        _phrase(knowledge_base, "medical_referral", seed=_phrase_seed(session, "medical_referral"))
        or knowledge_base.company.safety_disclaimer
    )
    # "а больно?"/"это нормально?" — бытовые вопросы, попавшие в MEDICAL_KEYWORDS вместе с
    # реально острыми сигналами (кровотечение, аллергия). Soft-offer текст (regulated_soft_offer)
    # для ВСЕХ них одинаково заканчивался "если срочно — звоните... в скорую (103)" — пугает на
    # безобидном вопросе. escalation_urgency_for()=calm только если В СООБЩЕНИИ НЕТ других
    # медицинских слов (безопасный дефолт на смешанных фразах вроде "болит и кровит").
    safe_context: dict[str, object] = {
        "force_direct_answer": True,
        "message_to_user": message_to_user,
        "handoff_message": message_to_user,
        "restricted_category": restricted_category,
        "referral_service": consultation_service.model_dump() if consultation_service else None,
        "extra_referral_service": growth_removal_service.model_dump() if growth_removal_service else None,
        "escalation_urgency": escalation_urgency_for(normalized_message),
    }
    # Живой баг (аудит §2026-08-22, F-11): "окей ладно дам телефон. 89991234567. кстати я
    # беременна это важно для приёма дерматолога?" — телефон в том же сообщении, что и
    # медицинский вопрос, но эта ветка возвращается раньше общей "if phone:" проверки ниже
    # по функции и никогда её не достигает — номер молча терялся, lead_created=false. Кладём
    # контакт в safe_context тем же способом, что и _contact_safe_context — chat_service.py
    # подхватывает его в TRANSFER_OPERATOR/REGULATED_ADVICE-ветке и создаёт лид, не теряя
    # при этом мягкий медицинский текст (soft-offer), в отличие от обычного ASK_CONTACT-пути.
    if phone:
        safe_context["contact"] = {
            "name": extract_name(message, phone, known_services=knowledge_base.services),
            "phone": phone,
        }
    return PolicyResult(
        action=PolicyAction.TRANSFER_OPERATOR,
        reason=PolicyReason.REGULATED_ADVICE,
        service_id=service.id if service else None,
        confidence=0.98,
        safe_context=safe_context,
        quick_actions=_medical_referral_quick_actions(
            consultation_service, extra_service=growth_removal_service
        ),
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
        or contains_keyword(normalized_message, _doctor_specialty_info_keywords(doctors))
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
    if not contains_keyword(normalized_message, LEAD_FOLLOWUP_SHORT_KEYWORDS) and not contains_day_or_time_lemma(
        normalized_message
    ):
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
# Живой репро (аудит §2026-08-22, F-02/F-03): для medical_advice/regulated_advice просто
# "нет hard-restricted keyword" — недостаточно безопасный критерий отмены medical_requested.
# Живой пример: "у меня аллергическая реакция что делать!!!" верно классифицировалось как
# regulated_advice (confidence 1.0), но оверрайд гасил это — "аллергическая" не было в
# MEDICAL_KEYWORDS дословно. Но и просто убрать эти два интента из SAFE_SERVICE_REQUEST_INTENTS
# нельзя — сломало бы test_escape_hatch_allows_safe_service_question_even_if_model_flags_regulated
# ("что нельзя после процедуры чистки лица" — классификатор иногда завышает обычный уходовый
# вопрос до regulated_advice, эскалировать его НЕ надо). Разница не в интенте, а в форме
# сообщения: реальная жалоба не похожа на известный FAQ-паттерн, обычный уходовый вопрос —
# похож. Для medical_advice/regulated_advice требуем ОБА признака: нет hard-restricted
# сигнала, И сообщение совпадает с распознанным FAQ-паттерном (FAQ_QUESTION_KEYWORDS) —
# не просто "не похоже на опасное", а "похоже на конкретный известный безопасный вопрос".
_MEDICAL_INTENT_SAFE_OVERRIDE_INTENTS = {"medical_advice", "regulated_advice"}


_hard_restricted_single_word_lemmas: set[str] | None = None


def _hard_restricted_lemma_set() -> set[str]:
    # Считаем один раз за процесс (не на каждое сообщение) — леммы самих ключевых слов не
    # меняются. Только однословные ключи: многословные фразы уже матчатся по последовательности
    # токенов (_contains_token_sequence) — там инфлексии одного слова внутри фразы не так
    # критичны, как для одиночного слова типа "опасно" против "опасен".
    global _hard_restricted_single_word_lemmas
    if _hard_restricted_single_word_lemmas is None:
        single_words = {
            keyword for keyword in (HARD_RESTRICTED_KEYWORDS | MEDICAL_KEYWORDS) if " " not in keyword
        }
        lemmas: set[str] = set()
        for word in single_words:
            lemmas.update(lemmatize_tokens(word))
        _hard_restricted_single_word_lemmas = lemmas
    return _hard_restricted_single_word_lemmas


def _has_hard_restricted_signal(normalized_message: str) -> bool:
    # Живой баг (аудит §2026-08-06): "насколько опасен ботокс если делать часто, может
    # накапливаться в организме" — "опасно" есть в MEDICAL_KEYWORDS, но не "опасен" (другая
    # словоформа, не подстрока). Классификатор верно тегнул regulated_advice (0.95), но эта
    # проверка возвращала False из-за пропущенной формы, что снимало medical_requested и
    # отправляло вопрос в общую RAG-ветку мимо эскалации и бренд-гарда. Быстрая подстрочная
    # проверка остаётся первой (бесплатно ловит буквальные совпадения); лемматизация — только
    # запасной, более медленный проход, если она ничего не нашла.
    if contains_keyword(normalized_message, HARD_RESTRICTED_KEYWORDS | MEDICAL_KEYWORDS):
        return True
    return contains_keyword_lemma(normalized_message, _hard_restricted_lemma_set())


def escalation_urgency_for(message: str) -> str:
    """calm/urgent для regulated_soft_offer — единая точка расчёта срочности.

    Живой баг (аудит §2026-08-22, "скорая 103" систематически, Топ-1): раньше это считалось
    инлайном только в _medical_referral_result (keyword-путь), а LLM-риск-путь в
    chat_service.py вызывал _regulated_soft_offer_response() вообще без urgent=,
    молча получая дефолт True — "скорая" на ЛЮБОЕ сообщение, попавшее именно в этот
    путь, независимо от реальной срочности текста. Единая функция — чтобы оба
    вызывающих пути не могли разойтись снова тем же образом.
    """

    return "urgent" if _has_acute_danger_signal(normalize_text(message)) else "calm"


def _has_acute_danger_signal(normalized_message: str) -> bool:
    """Раздел 5 скрипта резервирует "скорую" буквально для 4 категорий — сильная боль,
    кровотечение, аллергическая реакция, резкое ухудшение. Второй слой того же Топ-1
    (после фикса межходовой утечки): прежняя _is_benign_medical_signal требовала явного
    "смягчающего" слова, чтобы НЕ дать urgent — а MEDICAL_KEYWORDS широкий (там и "родинка",
    и "рецепт"), так что почти любое мед-окрашенное сообщение дефолтилось в urgent без
    единого признака реальной срочности: живые репро — голый ценовой вопрос "сколько стоит
    удаление родинки" и даже шутка "а вы умеете готовить рецепты? лол" ("рецепт" совпал с
    мед.термином) получали "скорая (103)". Теперь наоборот: urgent требует явного сигнала
    ИЗ ЭТИХ 4 категорий, а не "не доказано, что безобидно"."""

    if contains_keyword(normalized_message, ACUTE_BLEEDING_KEYWORDS):
        return True
    if contains_keyword(normalized_message, ACUTE_ALLERGY_KEYWORDS):
        return True
    if contains_keyword(normalized_message, ACUTE_DETERIORATION_KEYWORDS):
        return True
    return contains_keyword(normalized_message, PAIN_INTENSITY_KEYWORDS) and contains_keyword(
        normalized_message, PAIN_WORDS
    )


_NEGATIVE_RHETORICAL_PREFIXES = {"или", "либо"}


def _has_bare_negative_signal(normalized_message: str) -> bool:
    """Живой баг: 'ты реальный или нет' матчило NEGATIVE_MESSAGES буквально по слову 'нет' и
    отвечало 'Ок, ничего не оформляем' — хотя это риторический оборот ('или нет'/'либо нет',
    как 'так или нет?'/'будет или нет'), не отказ от чего-либо. contains_keyword не различает
    позицию совпадения — проверяем отдельно, что ни одно совпадение NEGATIVE_MESSAGES не идёт
    сразу после 'или'/'либо'."""

    if not contains_keyword(normalized_message, NEGATIVE_MESSAGES):
        return False
    tokens = normalized_message.split()
    for phrase in NEGATIVE_MESSAGES:
        phrase_tokens = phrase.split()
        span = len(phrase_tokens)
        for start in range(len(tokens) - span + 1):
            if not all(
                _keyword_token_matches(tokens[start + offset], phrase_tokens[offset])
                for offset in range(span)
            ):
                continue
            if start > 0 and tokens[start - 1] in _NEGATIVE_RHETORICAL_PREFIXES:
                continue
            return True
    return False


def _looks_like_safe_known_service_request(intent: str, normalized_message: str, service) -> bool:
    if service is None or intent not in SAFE_SERVICE_REQUEST_INTENTS:
        return False
    if _has_hard_restricted_signal(normalized_message):
        return False
    if intent in _MEDICAL_INTENT_SAFE_OVERRIDE_INTENTS:
        return contains_keyword(normalized_message, FAQ_QUESTION_KEYWORDS)
    return True


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
            # Живой баг (аудит §2026-08-22, F-12): "ксеомин это тоже самое что ботокс? и
            # сколько стоит на лоб" — guard верно блокирует обсуждение "ботокс", но дословно
            # игнорировал вложенный ценовой вопрос про разрешённый бренд (Ксеомин), давая
            # ОДИН И ТОТ ЖЕ текст на любой follow-up с упомянутым блокированным брендом,
            # включая явный вопрос о цене. Сама защита бренда не смягчается — просто не
            # оставляем ценовой вопрос совсем без ответа.
            if fuzzy_contains(normalized_message, PRICE_KEYWORDS):
                message_to_user += " Точную стоимость по разрешённым вариантам подскажет менеджер."
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


def _analyze_message_core(
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
    # Живой баг (2026-08-10): objection_price спрашивает "расскажу подробнее, что входит?",
    # пользователь отвечает голым "давай" — но это не содержит EXPLANATION_KEYWORDS, поэтому
    # шло в общий clarify вместо ответа на же собственное предложение бота. Считаем "давай"/
    # "да" подтверждением ИМЕННО этого предложения, только если последний ответ бота сам был
    # objection_handled (не в любой другой ситуации) и сообщение — ТОЛЬКО подтверждение, без
    # ничего сверху (иначе "давай запишемся" потеряло бы свой явный смысл).
    if (
        not explanation_requested
        and session.last_intent == PolicyReason.OBJECTION_HANDLED.value
        and normalized_message.strip() in AFFIRMATIVE_MESSAGES
    ):
        explanation_requested = True
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
    if (
        medical_requested
        and intent == "objection"
        and str(classification.get("context_topic") or "") == "pain_fear"
    ):
        # is_restricted_question() пересчитывает медицинский сигнал из СЫРОГО сообщения
        # независимо от переданной классификации ("больно" в MEDICAL_KEYWORDS) — без этого
        # исключения §4.5-объекшен (research.md #5) всё равно проваливался бы в эскалацию
        # здесь, даже когда chat_utils уже провалидировал, что другого мед-сигнала нет.
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
            # Живой баг (research.md #1): в отличие от unknown_service/off_topic/list_services,
            # эта ветка возвращала RAG-подсказку БЕЗ проверки _has_strong_article_overlap — одно
            # случайное общее слово ("процедуры") со статьёй про восстановление волос перекрывало
            # эскалацию на сообщении "лицо распухло, тяжело дышать". Тот же гейт, что и везде.
            if guidance_result is not None and _has_strong_article_overlap(
                normalized_message, guidance_result
            ):
                # Живой баг (research.md #4, третий аудит): каноничный пример §3.2 скрипта
                # ("выпадают волосы, не знаю к кому обращаться") сразу получал предложение
                # услуги — скрипт ожидает сначала короткий уточняющий вопрос, когда человек
                # описал СИМПТОМ (не назвал услугу) на первой реплике диалога.
                if _is_first_substantive_message(session) and not _curated_match_is_explicit_service_mention(
                    guidance_result, knowledge_base
                ):
                    return _symptom_followup_result(guidance_result, knowledge_base, session)
                return guidance_result
        return _medical_referral_result(
            message,
            normalized_message,
            knowledge_base,
            session,
            service,
            restricted_category,
            phone,
        )

    if contains_keyword(normalized_message, COMPLAINT_ESCALATION_KEYWORDS):
        # Живой баг (research.md #2, третий аудит): §5 скрипта требует немедленной передачи
        # оператору на жалобу/возврат денег/юридику/угрозу отзывом — раньше эти сообщения
        # перехватывались booking_request/price_question/off_topic ниже и не эскалировали
        # вообще. Приоритет — сразу после медицинской безопасности, до остальной классификации.
        return PolicyResult(
            action=PolicyAction.TRANSFER_OPERATOR,
            reason=PolicyReason.COMPLAINT,
            service_id=service.id if service else None,
            confidence=0.92,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": _phrase(knowledge_base, "complaint_escalation"),
                "handoff_message": _phrase(knowledge_base, "complaint_escalation"),
            },
            quick_actions=["Написать в Telegram", "Открыть сайт"],
        )

    # Живой баг (аудит §2026-08-06): "хотя нет забудьте, а сколько стоит биоревитализация
    # губ?" — классификация уже верно распознала price_question (0.86), но бывшая голая
    # проверка на "нет" срабатывала первой и полностью проглатывала вопрос ("Ок, ничего не
    # оформляем"), хотя отменять было нечего. Негативный сигнал считаем отменой только когда
    # в СООБЩЕНИИ нет никакого другого содержательного сигнала — иначе это не отказ, а просто
    # "нет" в начале фразы перед настоящим вопросом.
    #
    # Живой баг #2 (2026-08-10): изначально тут был allowlist конкретных интентов — "отмена,
    # покажи услуги" (list_services) и "отмена, не знаю к какому врачу" (doctor_uncertain,
    # добавленный в этой же сессии чуть раньше) молча гасились, потому что их забыли вписать
    # в список. Allowlist обязательно отстаёт от новых интентов — заменили на denylist
    # заведомо несодержательных интентов: если классификатор реально что-то понял (confidence
    # > 0) и это не пустая болтовня/офтоп/сам-непонятно-что — значит есть что перекрывать
    # отменой. Новые интенты (как doctor_uncertain сегодня) закрываются автоматически, без
    # необходимости вспоминать про этот список.
    has_competing_substantive_signal = (
        price_requested
        or booking_requested
        or duration_requested
        or explanation_requested
        or lead_requested
        or service is not None
        or (classifier_confidence > 0 and intent not in NEGATIVE_CATCHALL_EXCLUDED_INTENTS)
    )

    if (
        session.pending_action == PendingAction.COLLECT_CONTACT.value
        and _has_bare_negative_signal(normalized_message)
        and not has_competing_substantive_signal
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

    if (
        session.pending_action == PendingAction.BOOKING_CONTACT.value
        and _has_bare_negative_signal(normalized_message)
        and not has_competing_substantive_signal
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

    if _has_bare_negative_signal(normalized_message) and not has_competing_substantive_signal:
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
            quick_actions=["Утром", "Вечером", "Оставить телефон", "Позвать менеджера"],
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
                "message_to_user": _format_phrase(
                    knowledge_base,
                    "location_mismatch_offer",
                    seed=_phrase_seed(session, "location_mismatch_offer"),
                    city=city_in_text,
                )
                or f"Очный приём только в {city_in_text}. Уточните, пожалуйста, у менеджера, какой формат записи подойдёт в вашем случае.",
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
                    "value": "Уточнить формат записи",
                }
            ],
        )

    if intent == "objection":
        objection_result = _objection_result(knowledge_base, session, classification, classifier_confidence)
        if objection_result is not None:
            return objection_result

    if intent == "doctor_uncertain":
        # §3.4 скрипта: "не понимаю, к кому мне лучше попасть" — пациент не просит список
        # врачей (это отдельная doctor_info-ветка ниже), а не может сам выбрать специалиста.
        # Реиспользуем ту же generic-находку "консультации", что и медицинский реферал — не
        # называем конкретную специализацию (в данных клиники она заполнена только у
        # гинеколога), честная общая формулировка для остальных докторов.
        consultation_service = _consultation_service_for_referral(normalized_message, knowledge_base)
        return PolicyResult(
            action=PolicyAction.CLARIFY,
            reason=PolicyReason.OK,
            service_id=consultation_service.id if consultation_service else None,
            confidence=classifier_confidence or 0.88,
            safe_context={
                "force_direct_answer": True,
                "message_to_user": _phrase(
                    knowledge_base, "doctor_uncertain", seed=_phrase_seed(session, "doctor_uncertain")
                ),
            },
            quick_actions=_medical_referral_quick_actions(consultation_service),
        )

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
        if contains_keyword(normalized_message, LAB_TEST_KEYWORDS):
            return _lab_test_result(knowledge_base, session, classifier_confidence)

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

    # explanation_requested может быть True из-за "давай"/"да" после objection_handled (см.
    # выше) даже когда сырая классификация сообщения — "clarify" (у "давай" самого по себе
    # нет содержания вне контекста). Без этой оговорки generic-clarify перехватывал бы раньше,
    # чем код вообще доходил до explanation_requested-ветки ниже.
    if intent == "clarify" and not explanation_requested:
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
        if guidance_result is not None and _has_strong_article_overlap(
            normalized_message, guidance_result
        ):
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
        if contains_keyword(normalized_message, LAB_TEST_KEYWORDS):
            return _lab_test_result(knowledge_base, session, classifier_confidence)

        article_query = f"{service.name} {message}" if service is not None else message
        article_matches = _retrieve_article_context_safe(article_query)

        guidance_result = _cosmetic_article_guidance_result(
            knowledge_base,
            article_matches,
            normalized_message,
        )
        # Живой баг (2026-08-18, тот же класс, что чинили сегодня для "анализов"): это был
        # единственный из 6 вызовов _cosmetic_article_guidance_result без гейта
        # _has_strong_article_overlap — слабое семантическое совпадение по RAG-скору забирало
        # ответ вместо честного faq_question ниже. Тот же гейт, что и во всех остальных ветках.
        if guidance_result is not None and _has_strong_article_overlap(normalized_message, guidance_result):
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

        if contains_keyword(normalized_message, LAB_TEST_KEYWORDS):
            return _lab_test_result(knowledge_base, session, classifier_confidence)

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
            # Живой баг (research.md #4): реальная классификация для "выпадают волосы, не
            # знаю к кому обращаться" — unknown_service, не medical_advice. Тот же гейт §3.2,
            # что и в ветке medical_requested — иначе фикс не трогает фактический живой путь.
            if _is_first_substantive_message(session) and not _curated_match_is_explicit_service_mention(
                guidance_result, knowledge_base
            ):
                return _symptom_followup_result(guidance_result, knowledge_base, session)
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
            quick_actions=["Утром", "Вечером", "Оставить телефон", "Позвать менеджера"],
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

        # Живой баг (2026-08-10): "а что это"/"расскажи подробнее" для услуги, сгруппированной
        # из прайса, отвечали только по её short_description — а он для таких услуг часто
        # автосгенерированная заглушка вида "Направление «Мезотерапия». В прайсе 15 вариантов."
        # без единого слова о том, что это вообще такое. При этом для этой же услуги может уже
        # существовать куратированная статья с реальным описанием (её же использует соседняя
        # ветка faq_question/cosmetic_concern) — просто эта ветка про неё не знала. Предпочитаем
        # статью, если она для услуги есть; не гейтим _has_strong_article_overlap, т.к. это не
        # score-based совпадение по тексту, а прямая связь service_id → статья, уже подтверждённая
        # куратором на этапе одобрения.
        article_entry = _approved_article_for_service(knowledge_base, service.id)
        if article_entry is not None:
            article_result = _article_guidance_result_from_entry(knowledge_base, article_entry)
            if article_result is not None:
                return article_result

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
                        "Могу подсказать цену или передать заявку."
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
                "Скажите, пожалуйста, что вас интересует — услуга, цена или запись? "
                "Так отвечу точнее."
            )
        },
        quick_actions=["Посмотреть услуги", "Позвать менеджера"],
    )


def _looks_like_answer_ignoring_booking(result: PolicyResult, booking_requested: bool) -> bool:
    # ANSWER для завершённых ответов, CLARIFY для "вот варианты, какой интересует" (price-list
    # шаблон, где реально всплыл этот баг, размечен именно как CLARIFY, не ANSWER) — оба этих
    # экшена значат "дал содержательный текст", в отличие от reject/off_topic/small_talk/
    # transfer_operator, которым бридж про запись был бы неуместен.
    if not booking_requested or result.action not in (PolicyAction.ANSWER, PolicyAction.CLARIFY):
        return False
    # Ограничиваемся детерминированными force_direct_answer-ветками: у них message_to_user —
    # готовый финальный текст, который безопасно дополнить. LLM-ветки тут не трогаем — им уже
    # явно велено в BASE_SYSTEM_PROMPT не игнорировать второй вопрос молча, а текста на этом
    # уровне (до генерации) ещё нет, дополнять нечего.
    if not result.safe_context.get("force_direct_answer"):
        return False
    message_to_user = str(result.safe_context.get("message_to_user") or "")
    if not message_to_user:
        return False
    lower = message_to_user.lower()
    return "запис" not in lower and "заявк" not in lower


def _augment_dropped_booking_intent(
    result: PolicyResult, message: str, knowledge_base: KnowledgeBase
) -> PolicyResult:
    """Живой баг: составные сообщения вроде «сколько стоит эпиляция и можно ли записаться на
    завтра?» отвечали только на информационную часть (цена/срок/объяснение) — просьба
    записаться молча терялась. Тот же класс проблемы, что раньше нашли с потерей имени/города
    в составном сообщении, только на этот раз в детерминированных force_direct_answer-ветках
    (их в этом файле ~20) — точечно чинить каждую было бы игрой в вихрь, поэтому один общий
    пост-чек поверх результата _analyze_message_core вместо правки каждой ветки по отдельности.
    """
    normalized_message = normalize_text(message)
    booking_requested = contains_keyword(normalized_message, BOOKING_KEYWORDS)
    if not _looks_like_answer_ignoring_booking(result, booking_requested):
        return result

    bridge = _phrase(knowledge_base, "booking_bridge") or (
        "Записаться тоже можно — оставьте телефон и удобное время, менеджер подтвердит."
    )
    message_to_user = str(result.safe_context.get("message_to_user") or "")
    new_context = dict(result.safe_context)
    new_context["message_to_user"] = f"{message_to_user} {bridge}"

    quick_actions = list(result.quick_actions)
    if "Оставить телефон" not in quick_actions:
        quick_actions.append("Оставить телефон")

    return PolicyResult(
        action=result.action,
        reason=result.reason,
        service_id=result.service_id,
        confidence=result.confidence,
        safe_context=new_context,
        quick_actions=quick_actions,
    )


def analyze_message(
    message: str,
    session: Session,
    knowledge_base: KnowledgeBase,
    classification: Optional[dict[str, object]] = None,
) -> PolicyResult:
    """классифицирует сообщение до любого взаимодействия с llm.

    Тонкая обёртка вокруг _analyze_message_core: применяет общий пост-чек поверх ЛЮБОЙ ветки
    (см. _augment_dropped_booking_intent) вместо точечных правок внутри каждой из них.
    """

    result = _analyze_message_core(message, session, knowledge_base, classification)
    return _augment_dropped_booking_intent(result, message, knowledge_base)
