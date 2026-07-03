

from __future__ import annotations

import hashlib
from typing import Any, Optional

from ..knowledge import normalize_text
from ..models import Message
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
                "Я AI-консультант центра. Помогу с услугами, ценами, записью или передам вопрос специалисту.",
                "Я помощник на сайте центра: могу сориентировать по услугам, ценам и записи.",
                "Я консультант в этом чате. Подскажу по базе центра или помогу позвать специалиста.",
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
                "Я на связи. Могу помочь по теме центра: услуги, цены, запись или специалист.",
                "Работаю тут навигатором по услугам центра. Напишите, что хотите узнать.",
                "Помогаю быстро разобраться с услугами, ценами и записью. С чего начнём?",
            ],
        )

    if "помоги" in normalized_message or "начнем" in normalized_message or "начнём" in normalized_message:
        return _choose_stable_variant(
            normalized_message,
            [
                "Конечно. Напишите услугу или вопрос, и я сориентирую по информации центра.",
                "Давайте. Можете спросить про услугу, цену, запись или специалиста.",
                "Начнём. Что интересует: услуги, стоимость, запись или связь со специалистом?",
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
                "Здравствуйте. Можете написать, какая услуга интересует, я подскажу по базе центра.",
                "Добрый день. Помогу с услугами, ценами, записью или передам вопрос специалисту.",
            ],
        )

    return _choose_stable_variant(
        normalized_message,
        [
            f"Я здесь, чтобы помочь по услугам центра. {SMALL_TALK_SERVICE_PIVOT}",
            "Могу сориентировать по услугам центра, ценам или записи.",
            "Напишите, что хотите узнать по центру: услугу, цену, запись или специалиста.",
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
        variants = [
            f"Понял, вы про «{service_name}». Могу подсказать стоимость или позвать специалиста.",
            f"Да, «{service_name}» есть в базе центра. Могу сориентировать по цене или передать вопрос специалисту.",
            f"По услуге «{service_name}» могу помочь в рамках информации центра. Если нужно, уточним стоимость или позовём специалиста.",
            f"Могу коротко сориентировать по «{service_name}»: дальше удобнее уточнить цену или передать вопрос специалисту.",
            f"Если речь про «{service_name}», я могу помочь с общей информацией центра или подключить специалиста.",
        ]
        return _choose_stable_variant(seed, variants)

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
                f"Понимаю, хочется подобрать уход под этот запрос. В центре есть близкие направления: {service_text}; точнее сориентирует специалист.",
                f"Для такого запроса можно начать с консультации и посмотреть близкие услуги: {service_text}. Если хотите, передам вопрос специалисту.",
                f"Тут лучше не угадывать заочно. Могу показать близкие услуги ({service_text}) или позвать специалиста.",
                f"По такому запросу лучше идти аккуратно: могу показать близкие услуги ({service_text}) или передать вопрос специалисту.",
            ]
            return _choose_stable_variant(seed + service_text, variants)

    return "Понял запрос. Могу подсказать по стоимости или передать вопрос специалисту."


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
        "препарат",
        "таблет",
        "мазь",
        "осложнение",
        "осложнения",
        "покраснение",
        "отек",
        "отёк",
        "температура",
        "сыпь",
        "прыщ",
        "прыщи",
        "акне",
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
        "какая процедура от",
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
        del system_prompt, user_message, history

        message_to_user = context.get("message_to_user")
        if isinstance(message_to_user, str) and message_to_user.strip():
            return message_to_user.strip()

        service = context.get("service") or {}
        price = context.get("price") or {}
        phrasebook = context.get("phrasebook") if isinstance(context.get("phrasebook"), dict) else {}
        price_disclaimer = str(phrasebook.get("price_disclaimer") or PRICE_DISCLAIMER)
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
                    + ". Точные рекомендации даст специалист на консультации."
                )

        service_name = service.get("name")
        short_description = service.get("short_description")
        duration = service.get("duration")
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
                parts.append("Точную длительность уточнит специалист.")
        elif question_type == "explanation" and variant_examples:
            parts.append("Примеры позиций: " + "; ".join(variant_examples) + ".")
            parts.append("Точный вариант и стоимость лучше подтвердить со специалистом.")
        elif price.get("price_text"):
            if variants_count:
                parts.append(
                    f"Стоимость зависит от параметров услуги: {price['price_text']}. {price_disclaimer}"
                )
            else:
                parts.append(f"Стоимость: {price['price_text']}. {price_disclaimer}")
        elif service_name:
            parts.append("Точные детали по стоимости и длительности уточнит специалист.")

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
