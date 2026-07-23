

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from ..knowledge import normalize_text
from ..leads import REASON_LABELS
from ..models import Lead, Message, Session
from .base import BaseLLMClient
from .classification import IntentClassification, normalize_intent_classification
from .prompts import (
    DEFAULT_FALLBACK,
    PRICE_DISCLAIMER,
    RESTRICTED_HANDOFF_FALLBACK,
    SMALL_TALK_SERVICE_PIVOT,
)


def _choose_stable_variant(seed: str, variants: list[str]) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    index = int(digest[:8], 16) % len(variants)
    return variants[index]


def _clean_article_sentence(value: str) -> str:
    value = re.sub(r"(?:₽|руб(?:лей|ля|ль|\.)?)", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\d+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _price_disclaimer_variant(seed: str, phrasebook_disclaimer: str) -> str:
    if phrasebook_disclaimer and phrasebook_disclaimer != PRICE_DISCLAIMER:
        return phrasebook_disclaimer
    return _choose_stable_variant(
        seed,
        [
            PRICE_DISCLAIMER,
            "Это предварительная стоимость. Точную сумму подтвердит менеджер после уточнения деталей.",
            "Окончательную стоимость менеджер уточнит после деталей.",
        ],
    )


def small_talk_template(user_message: str) -> str:
    normalized_message = normalize_text(user_message)

    if "спасибо" in normalized_message or "благодарю" in normalized_message:
        return _choose_stable_variant(
            normalized_message,
            [
                f"Пожалуйста. Если нужно — {SMALL_TALK_SERVICE_PIVOT.lower()}",
                "Рад помочь. Можете написать услугу или вопрос по записи.",
                "Не за что. Если захотите, сориентирую по услугам центра.",
            ],
        )

    if "ты кто" in normalized_message or "кто ты" in normalized_message:
        return _choose_stable_variant(
            normalized_message,
            [
                "Я AI-консультант центра. Помогу с услугами, ценами, записью или передам вопрос менеджеру.",
                "Я помощник на сайте центра: могу сориентировать по услугам, ценам и записи.",
                "Я консультант в этом чате. Подскажу по услугам центра или помогу позвать менеджера.",
            ],
        )

    if (
        "как дела" in normalized_message
        or "что делаешь" in normalized_message
        or "чем занимаешься" in normalized_message
    ):
        return _choose_stable_variant(
            normalized_message,
            [
                "Я на связи. Могу помочь по теме центра: услуги, цены, запись или менеджер.",
                "Работаю тут навигатором по услугам центра. Напишите, что хотите узнать.",
                "Помогаю быстро разобраться с услугами, ценами и записью. С чего начнём?",
            ],
        )

    if "помоги" in normalized_message or "начнем" in normalized_message or "начнём" in normalized_message:
        return _choose_stable_variant(
            normalized_message,
            [
                "Конечно. Напишите услугу или вопрос, и я сориентирую по информации центра.",
                "Давайте. Можете спросить про услугу, цену, запись или менеджера.",
                "Начнём. Что интересует: услуги, стоимость, запись или связь с менеджером?",
            ],
        )

    if any(
        greeting in normalized_message.split()
        for greeting in {"привет", "здравствуй", "хай", "ку"}
    ) or any(
        greeting in normalized_message
        for greeting in {"добрый день", "добрый вечер", "доброе утро"}
    ):
        return _choose_stable_variant(
            normalized_message,
            [
                f"Здравствуйте. Я на связи. {SMALL_TALK_SERVICE_PIVOT}",
                "Здравствуйте. Можете написать, какая услуга интересует, я подскажу по услугам центра.",
                "Добрый день. Помогу с услугами, ценами, записью или передам вопрос менеджеру.",
            ],
        )

    return _choose_stable_variant(
        normalized_message,
        [
            f"Я здесь, чтобы помочь по услугам центра. {SMALL_TALK_SERVICE_PIVOT}",
            "Могу сориентировать по услугам центра, ценам или записи.",
            "Напишите, что хотите узнать по центру: услугу, цену, запись или менеджера.",
        ],
    )


def service_consultation_template(
    context: dict[str, Any],
    user_message: str,
    history: Optional[list[Message]] = None,
) -> str:
    service = context.get("service") if isinstance(context.get("service"), dict) else {}
    service_name = str(service.get("name") or "").strip()
    recent_user_messages = [
        message.text
        for message in (history or [])[-4:]
        if message.role.value == "user"
    ]
    seed = "|".join([service_name, user_message, *recent_user_messages])
    if service_name:
        return _choose_stable_variant(seed, _service_mention_variants(service_name, service))

    suggested_services = context.get("suggested_services")
    if isinstance(suggested_services, list) and suggested_services:
        names = [
            str(item.get("name"))
            for item in suggested_services
            if isinstance(item, dict) and item.get("name")
        ]
        if names:
            service_text = ", ".join(names)
            variants = [
                f"Понимаю, хочется подобрать уход под этот запрос. В центре есть близкие направления: {service_text}; точнее сориентирует менеджер.",
                f"Для такого запроса можно начать с консультации и посмотреть близкие услуги: {service_text}. Если хотите, передам вопрос менеджеру.",
                f"Тут лучше не угадывать заочно. Могу показать близкие услуги ({service_text}) или позвать менеджера.",
                f"По такому запросу лучше идти аккуратно: могу показать близкие услуги ({service_text}) или передать вопрос менеджеру.",
            ]
            return _choose_stable_variant(seed + service_text, variants)

    return "Понял запрос. Могу подсказать по стоимости или передать вопрос менеджеру."


def _service_mention_variants(service_name: str, service: dict[str, Any]) -> list[str]:
    variants_payload = service.get("variants")
    variants_count = len(variants_payload) if isinstance(variants_payload, list) else 0
    suppress_variant_examples = service.get("suppress_variant_examples") is True
    variant_examples = []
    if not suppress_variant_examples:
        variant_examples = [
            item.split(" — ", 1)[0]
            for item in _variant_examples(service, limit=2)
        ]

    variants = [
        f"«{service_name}» есть у нас.",
        f"Да, «{service_name}» — одно из направлений центра.",
        f"По услуге «{service_name}» могу подсказать основную информацию.",
        f"«{service_name}» есть в центре, детали лучше уточнять по конкретному запросу.",
    ]

    if variants_count > 1:
        variants.append(f"«{service_name}» — направление с {variants_count} вариантами в прайсе.")
        variants.append(f"По «{service_name}» есть несколько вариантов.")
        if variant_examples:
            variants.append(f"По «{service_name}» среди вариантов есть «{variant_examples[0]}».")
        if len(variant_examples) > 1:
            variants.append(
                f"В «{service_name}» входят разные позиции, например «{variant_examples[0]}» и «{variant_examples[1]}»."
            )
    else:
        variants.extend(
            [
                f"Такая услуга есть: «{service_name}». Могу подсказать цену, детали или передать вопрос менеджеру.",
                f"«{service_name}» есть в списке услуг центра.",
                f"По «{service_name}» могу помочь в рамках информации центра.",
            ]
        )

    return variants


def _variant_examples(service: dict[str, Any], *, limit: int = 4) -> list[str]:
    variants = service.get("variants")
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


def medical_risk_template(user_message: str) -> str:
    normalized_message = normalize_text(user_message)
    medical_markers = {
        "болит",
        "боль",
        "кровит",
        "кровоточит",
        "кровоточить",
        "кровоточение",
        "кровь",
        "кровотечение",
        "родинка",
        "опасно",
        "аллергия",
        "аллергии",
        "беременна",
        "беременность",
        "лекарства",
        "таблет",
        "мазь",
        "осложнение",
        "осложнения",
        "покраснение",
        "отек",
        "отёк",
        "температура",
        "сыпь",
        "диагноз",
        "лечение",
        "лечить",
    }
    advice_markers = {
        "что посоветуете",
        "что делать",
        "это нормально",
        "нормально ли",
        "можно ли",
    }
    if any(marker in normalized_message for marker in medical_markers | advice_markers):
        return "MEDICAL"
    return "COSMETIC"


class MockLLMClient(BaseLLMClient):
    """детерминированный сборщик ответов, если внешняя llm не настроена."""

    async def complete(
        self,
        system_prompt: str,
        context: dict[str, Any],
        user_message: str,
        history: list[Message],
    ) -> str:
        del system_prompt, history

        message_to_user = context.get("message_to_user")
        if isinstance(message_to_user, str) and message_to_user.strip():
            return message_to_user.strip()

        service = context.get("service") or {}
        price = context.get("price") or {}
        phrasebook = context.get("phrasebook") if isinstance(context.get("phrasebook"), dict) else {}
        question_type = context.get("question_type")
        all_services = context.get("all_services")
        suggested_services = context.get("suggested_services")

        if question_type == "list_services" and isinstance(all_services, list):
            service_names = [
                str(service.get("name"))
                for service in all_services
                if isinstance(service, dict) and service.get("name")
            ]
            if service_names:
                return "В центре доступны услуги: " + ", ".join(service_names) + ". Какая услуга вас интересует?"

        if question_type == "cosmetic_concern" and isinstance(suggested_services, list):
            service_names = [
                str(service.get("name"))
                for service in suggested_services
                if isinstance(service, dict) and service.get("name")
            ]
            if service_names:
                return (
                    "Для такого запроса обычно подходят: "
                    + ", ".join(service_names)
                    + ". Точные рекомендации даст менеджер на консультации."
                )

        if question_type == "faq_question":
            article_context = context.get("article_context")
            if isinstance(article_context, list) and article_context:
                first_match = article_context[0]
                if isinstance(first_match, dict):
                    title = _clean_article_sentence(str(first_match.get("title") or "статьи"))
                    snippet = str(first_match.get("snippet") or "").strip()
                    if snippet:
                        first_sentence = _clean_article_sentence(snippet.split(". ")[0])
                        if first_sentence and not first_sentence.endswith("."):
                            first_sentence += "."
                        return f"По данным статьи «{title}»: {first_sentence} Подробности уточнит менеджер."
            return "По этому вопросу лучше уточнить у менеджера — подключить?"

        if question_type == "article_guidance_excerpt":
            guidance = context.get("article_guidance_candidate")
            guidance = guidance if isinstance(guidance, dict) else {}
            excerpt = _clean_article_sentence(str(guidance.get("excerpt") or ""))
            service_names = str(guidance.get("service_names") or "").strip()
            if excerpt:
                if not excerpt.endswith("."):
                    excerpt += "."
                if service_names:
                    return (
                        f"В материалах центра по этой теме указано: {excerpt} "
                        f"С этим связаны услуги: {service_names}. "
                        "Точный подбор подтвердит специалист на консультации."
                    )
                return (
                    f"В материалах центра по этой теме указано: {excerpt} "
                    "Точный подбор подтвердит специалист на консультации."
                )
            return "По этому вопросу лучше уточнить у менеджера — подключить?"

        service_name = service.get("name")
        short_description = service.get("short_description")
        duration = service.get("duration")
        price_unit_note = str(context.get("price_unit_note") or "").strip()
        price_disclaimer = _price_disclaimer_variant(
            f"{question_type}:{service_name}:{user_message}",
            str(phrasebook.get("price_disclaimer") or ""),
        )
        variants_count = len(service.get("variants")) if isinstance(service.get("variants"), list) else 0
        variant_examples = _variant_examples(service)

        parts: list[str] = []
        if service_name:
            if variants_count:
                parts.append(f"{service_name} — направление с {variants_count} вариантами в прайсе.")
            else:
                parts.append(f"{service_name} — {short_description}")
        if question_type == "duration":
            if duration:
                parts.append(f"Длительность: {duration}. {price_disclaimer}")
            else:
                parts.append("Точную длительность уточнит менеджер.")
        elif question_type == "explanation" and variant_examples:
            parts.append("Примеры позиций: " + "; ".join(variant_examples) + ".")
            parts.append("Точный вариант и стоимость лучше подтвердить с менеджером.")
        elif price.get("price_text"):
            if variants_count:
                parts.append(
                    f"Стоимость зависит от параметров услуги: {price['price_text']}. {price_disclaimer}"
                )
            else:
                parts.append(f"Стоимость: {price['price_text']}. {price_disclaimer}")
            if price_unit_note:
                parts.append(price_unit_note)
        elif service_name:
            parts.append("Точные детали по стоимости и длительности уточнит менеджер.")

        if parts:
            return " ".join(parts)

        return DEFAULT_FALLBACK

    async def small_talk(self, company_name: str, user_message: str) -> str:
        del company_name
        return small_talk_template(user_message)

    async def service_consultation(
        self,
        context: dict[str, Any],
        user_message: str,
        history: Optional[list[Message]] = None,
    ) -> str:
        return service_consultation_template(context, user_message, history)

    async def classify_restricted_risk(self, user_message: str) -> str:
        return medical_risk_template(user_message)

    async def restricted_handoff(self, user_message: str) -> str:
        del user_message
        return RESTRICTED_HANDOFF_FALLBACK

    async def classify_medical_risk(self, user_message: str) -> str:
        return await self.classify_restricted_risk(user_message)

    async def medical_handoff(self, user_message: str) -> str:
        return await self.restricted_handoff(user_message)

    async def summarize_session(self, session: Session, lead: Lead) -> str:
        last_user_text = ""
        for message in reversed(session.messages):
            if message.role.value == "user" and message.text.strip():
                last_user_text = message.text.strip()
                break

        reason_label = REASON_LABELS.get(lead.reason, "Оператор/контакт")
        if last_user_text and last_user_text not in lead.summary:
            return f"{reason_label}: {lead.summary} Последний вопрос: {last_user_text}"
        return f"{reason_label}: {lead.summary}"

    async def classify_and_extract(
        self,
        user_message: str,
        known_services: list[dict[str, str]],
    ) -> dict[str, object]:
        from ..policy import classify_and_extract

        return classify_and_extract(user_message, known_services)

    async def classify_structured(
        self,
        user_message: str,
        known_services: list[dict[str, str]],
        domain_profile: dict[str, Any] | None = None,
    ) -> IntentClassification | None:
        from ..policy import classify_and_extract

        legacy_result = classify_and_extract(
            user_message,
            known_services,
            domain_profile=domain_profile,
        )
        intent = str(legacy_result.get("intent") or "service_mention")
        service_id = legacy_result.get("service_id")
        confidence = float(legacy_result.get("confidence") or 0.0)
        if intent == "medical_advice":
            raw_result = {
                "intent": "regulated_advice",
                "risk": "regulated_advice",
                "service_match_type": "none",
                "confidence": confidence,
                "reason_code": "legacy_medical_advice",
            }
        else:
            raw_result = {
                "intent": intent,
                "risk": "off_topic" if intent == "off_topic" else "safe",
                "service_id": service_id,
                "service_match_type": "exact" if service_id else "none",
                "confidence": confidence,
                "reason_code": f"legacy_{intent}",
            }
        known_service_ids = {
            str(service.get("id"))
            for service in known_services
            if service.get("id")
        }
        return normalize_intent_classification(raw_result, known_service_ids)
