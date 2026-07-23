"""загрузка базы знаний и вспомогательные функции для поиска."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yaml

from .models import ArticleServiceMapEntry, CompanyConfig, PriceEntry, Service


logger = logging.getLogger(__name__)
CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
KB_REQUIRED_FILES = ("company.yaml", "services.json", "prices.json", "faq.md")
PhrasebookValue = str | list[str]
DEFAULT_WIDGET_FEATURES = {
    "operator": True,
    "lead_capture": True,
    "analytics": False,
    "voice_input": True,
}
DEFAULT_WIDGET_CONFIG = {
    "primary_color": "#1F7A5C",
    "button_color": "#1F7A5C",
    "header_title": "Чат с поддержкой",
    "header_subtitle": "Подскажем по услугам и ценам",
    "position": "bottom-right",
    "avatar_emoji": "💬",
}
DEFAULT_DOMAIN_PROFILE = {
    "type": "generic",
    "safety_level": "normal",
    "regulated_escalation": "soft",
    "regulated_lead_mode": "flagged",
    "restricted_advice": [],
    "hard_block_topics": [
        "self_harm",
        "illegal_requests",
        "prompt_injection",
    ],
    "escalation_policy": {
        "unknown_service": "clarify_or_operator",
        "complaint": "operator",
        "custom_price": "operator",
        "booking": "lead_then_operator",
    },
}
DEFAULT_PHRASEBOOK = {
    "company_noun": "компания",
    "service_word": "услуга",
    "operator_label": "менеджер",
    "booking_word": "заявка",
    "greeting": [
        "Здравствуйте! Я AI-консультант компании. Подскажу по услугам, ценам и записи.",
        "Добрый день! Я на связи — спрашивайте про услуги, цены или запись, помогу разобраться.",
        "Привет! Я консультант компании — расскажу про услуги и цены, помогу с записью.",
    ],
    "price_disclaimer": "Предварительно так, точнее подскажет менеджер после уточнения деталей.",
    "unknown_service": [
        "В доступных мне данных подтверждения по этой услуге не нашёл — уточнить у менеджера или показать похожие варианты?",
        "Такого конкретно в прайсе не увидел, но по этой теме могут быть похожие процедуры. Показать список или спросить менеджера?",
        "У меня сейчас нет надёжных данных по этому вопросу. Могу показать актуальные услуги или подключить менеджера.",
    ],
    "unknown_service_named": [
        "«{service}» у нас не значится, но по этой теме могут быть похожие варианты. Показать?",
        "Конкретно «{service}» не нашёл в прайсе — могу показать похожие процедуры или подключить менеджера.",
        "По запросу «{service}» подтверждения в данных нет. Показать актуальные услуги или передать менеджеру?",
    ],
    "off_topic": [
        "С этим вопросом не помогу — я отвечаю по услугам, ценам и записи в центр. Подсказать по услугам?",
        "Я могу ориентировать только по данным компании: услуги, цены, запись, контакты. Что из этого подсказать?",
        "Это вне моей темы. Могу показать услуги центра или подключить менеджера.",
    ],
    "contact_prompt": "Оставьте имя и телефон, и менеджер сможет связаться с вами позже.",
    "booking_contact_prompt": "Чтобы оставить заявку, напишите имя, телефон и удобное время. Мы передадим заявку, а менеджер подтвердит детали.",
    "handoff_message": "Передаю диалог менеджеру. Можете дописать детали, он увидит историю.",
    "operator_soft_offer": "Могу попробовать помочь здесь — опишите, что вас интересует. Или сразу соединю с менеджером.",
    "regulated_soft_offer": [
        (
            "Чтобы не дать неточную информацию, лучше передам это специалисту — он разберёт по деталям. "
            "Подключить сейчас, или если срочно — звоните нам напрямую или в скорую (103)?"
        ),
        (
            "По переписке это безопасно не оценить — здесь нужен специалист. "
            "Оставить контакт, подключить менеджера сейчас, или при срочности звоните нам напрямую либо в скорую (103)?"
        ),
        (
            "Здесь важны детали, которые должен оценить специалист. "
            "Передать вопрос менеджеру или помочь оставить контакт? Если срочно — звоните нам напрямую или в скорую (103)."
        ),
    ],
    "engagement_offer_1": (
        "Вижу, диалог уже длинный — хотите, чтобы дальше подключился администратор, "
        "или продолжим здесь?"
    ),
    "engagement_offer_2": (
        "Если удобнее — могу передать администратору краткое резюме нашего разговора, "
        "он подключится быстрее."
    ),
    "engagement_offer_3": "Ещё раз на всякий случай предложу: подключить администратора, чтобы не тянуть?",
    "engagement_continue": "Хорошо, продолжим здесь. Напишите, что хотите уточнить дальше.",
    "clarify": "Уточните, пожалуйста, что вас интересует? Могу рассказать про услуги, цены или оформить заявку.",
    "empty_message": "Похоже, сообщение пустое. Напишите вопрос, и я подскажу.",
    "empty_message_letters": "Не совсем понял вопрос. Можете переформулировать словами?",
    "rate_limit": "Похоже, разговор затянулся. Если остались вопросы — оставьте телефон, мы свяжемся, или попробуйте начать новый диалог.",
    "human_active_wait": "Чат передан менеджеру. Пожалуйста, дождитесь ответа.",
    "contact_cancelled": "Ок, контакт не оставляем. Могу подсказать по услугам, ценам или позвать менеджера.",
    "booking_cancelled": "Ок, заявку не оформляем. Могу подсказать по услугам, ценам или позвать менеджера.",
    "general_cancelled": "Ок, ничего не оформляем. Могу подсказать по услугам, ценам или позвать менеджера.",
    "lead_success": "Спасибо. Передали ваши контакты менеджеру. С вами свяжутся для уточнения деталей.",
    "booking_success": "Спасибо. Заявку передали. С вами свяжутся, чтобы подтвердить время и детали.",
    "clinic_location": (
        "{company_name}: {city}. Адрес: {address}. Часы работы: {working_hours}. "
        "Если нужно, менеджер подскажет как добраться."
    ),
    "clinic_location_deferred": (
        "{company_name}: {city}. Часы работы: {working_hours}. "
        "Точный адрес уточнит менеджер."
    ),
    "doctors_from_data": "По данным центра: {doctors}. Точное расписание уточнит менеджер.",
    "doctors_deferred": (
        "Врачей по именам и профилю уточнит менеджер. "
        "Оставьте контакт — свяжемся и подскажем подходящего специалиста."
    ),
    "doctor_schedule_deferred": (
        "Центр работает: {working_hours}. "
        "Точное расписание конкретного врача уточнит менеджер."
    ),
    "doctor_schedule_from_data": "Расписание — {schedule}. Запись ведёт менеджер.",
    "fact_oms_no": "По ОМС приём не ведём, услуги доступны платно. Детали подскажет менеджер.",
    "fact_oms_yes": "Приём по ОМС возможен. Условия и документы уточнит менеджер.",
    "fact_dms_no": "По ДМС приём не ведём, услуги доступны платно. Детали подскажет менеджер.",
    "fact_dms_yes": "По ДМС приём возможен при наличии партнёрской страховой. Условия уточнит менеджер.",
    "fact_ambulance_no": (
        "Скорая помощь везёт в дежурный стационар по показаниям, не в частную клинику. "
        "По срочному вопросу уточните у менеджера."
    ),
    "fact_ambulance_yes": "Возможность доставки скорой нужно уточнить у менеджера по вашей ситуации.",
    "fact_products_no": "Продаём только услуги. По товарам или домашнему уходу детали уточнит менеджер.",
    "fact_products_yes": "Товары можно приобрести в центре. Наличие и цену уточнит менеджер.",
    "clinic_fact_deferred": [
        "По этому факту в базе нет подтверждённых данных — уточнит менеджер. Подключить его?",
        "Не нашёл подтверждения по этому вопросу в данных центра. Передать менеджеру?",
        "Чтобы не сказать неточно, этот факт уточнит менеджер. Оставить контакт?",
    ],
    "equipment_deferred": [
        "Конкретную модель аппарата уточнит менеджер — в базе нет подтверждённого названия.",
        "По оборудованию в базе нет подтверждённой модели. Уточнит менеджер — подключить?",
        "Название аппарата лучше подтвердит менеджер: в доступных данных его нет.",
    ],
    "medical_referral": [
        (
            "Это как раз то, с чем к нам часто обращаются — но точную причину и подходящий вариант "
            "определит специалист на очной консультации. Записать на консультацию?"
        ),
        (
            "Здесь важны детали, которые видно только на очной консультации — специалист посмотрит и подскажет точнее. "
            "Подключить менеджера сейчас?"
        ),
        "По переписке это безопасно не оценить — это вопрос для очной консультации специалиста. Помочь с записью?",
    ],
    "sensitive_escalate": [
        "Понимаю, тема деликатная — обсуждать её лучше лично со специалистом, не в переписке. Помочь оставить контакт?",
        "Это деликатная тема, и здесь важны детали очной консультации. Передать менеджеру?",
        "По такой деликатной теме безопаснее обсудить всё со специалистом очно. Подключить менеджера?",
    ],
    "sensitive_decline": [
        "Эту процедуру мы не проводим. Могу подсказать по другим услугам или передать менеджеру.",
        "По этой теме центр не оказывает услугу. Могу показать другие направления или подключить менеджера.",
        "Такую процедуру подтвердить не могу: в доступных данных её нет среди услуг центра. Уточнить у менеджера?",
    ],
    "efficacy_claim_deferred": (
        "Что подойдёт именно в вашем случае, определит специалист на очной консультации. "
        "Могу записать на консультацию или передать менеджеру."
    ),
    "lead_followup": "Заявку уже передали менеджеру — он уточнит удобное время и детали.",
    "lead_followup_service": (
        "Заявку уже передали менеджеру, включая интерес к услуге «{service_name}» — "
        "он уточнит удобное время и детали."
    ),
}


def _default_phrasebook() -> dict[str, PhrasebookValue]:
    return {
        key: list(value) if isinstance(value, list) else value
        for key, value in DEFAULT_PHRASEBOOK.items()
    }


def _normalize_phrasebook_value(value: object) -> PhrasebookValue | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, list):
        items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        return items or None
    return None


def _stable_choice_index(seed: str, options_len: int) -> int:
    if options_len <= 0:
        return 0
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return int(digest, 16) % options_len


def phrasebook_value_to_text(value: object, fallback: str = "", seed: str | None = None) -> str:
    if isinstance(value, str):
        return value.strip() or fallback
    if isinstance(value, list):
        items = [item.strip() for item in value if isinstance(item, str) and item.strip()]
        if items:
            if seed:
                return items[_stable_choice_index(seed, len(items))]
            return random.choice(items)
    return fallback


def _default_domain_profile() -> dict[str, object]:
    return {
        **DEFAULT_DOMAIN_PROFILE,
        "restricted_advice": list(DEFAULT_DOMAIN_PROFILE["restricted_advice"]),
        "hard_block_topics": list(DEFAULT_DOMAIN_PROFILE["hard_block_topics"]),
        "escalation_policy": dict(DEFAULT_DOMAIN_PROFILE["escalation_policy"]),
    }


def _domain_profile_from_payload(payload: dict[str, object]) -> dict[str, object]:
    profile = _default_domain_profile()
    raw_profile = payload.get("domain_profile") if isinstance(payload, dict) else None
    if not isinstance(raw_profile, dict):
        return profile

    for key in ("type", "safety_level"):
        value = raw_profile.get(key)
        if isinstance(value, str) and value.strip():
            profile[key] = value.strip()

    regulated_escalation = raw_profile.get("regulated_escalation")
    if isinstance(regulated_escalation, str) and regulated_escalation.strip() in {"soft", "instant"}:
        profile["regulated_escalation"] = regulated_escalation.strip()

    regulated_lead_mode = raw_profile.get("regulated_lead_mode")
    if isinstance(regulated_lead_mode, str) and regulated_lead_mode.strip() in {"flagged", "normal"}:
        profile["regulated_lead_mode"] = regulated_lead_mode.strip()

    for key in ("restricted_advice", "hard_block_topics"):
        value = raw_profile.get(key)
        if isinstance(value, list):
            profile[key] = [str(item).strip() for item in value if str(item).strip()]

    escalation_policy = raw_profile.get("escalation_policy")
    if isinstance(escalation_policy, dict):
        profile["escalation_policy"] = {
            **dict(profile["escalation_policy"]),
            **{
                str(key): str(value)
                for key, value in escalation_policy.items()
                if str(key).strip() and str(value).strip()
            },
        }

    return profile


def normalize_text(value: str) -> str:
    """нормализует текст для нечёткого поиска по ключевым словам."""

    normalized = value.lower().replace("ё", "е")
    normalized = re.sub(r"[^a-zа-я0-9\s]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _tokens(value: str) -> list[str]:
    return [token for token in normalize_text(value).split() if token]


def hostname_from_origin(value: str | None) -> str | None:
    """достаёт hostname из origin/referer/domain строки."""

    if not value:
        return None

    parsed = urlparse(value)
    if parsed.hostname:
        return parsed.hostname.lower().rstrip(".")

    parsed = urlparse(f"//{value}")
    if parsed.hostname:
        return parsed.hostname.lower().rstrip(".")

    return value.split("/", 1)[0].split(":", 1)[0].lower().rstrip(".") or None


def normalize_domain(value: str) -> str:
    return (hostname_from_origin(value) or value).strip().lower().rstrip(".")


def domain_matches(hostname: str, allowed_domain: str) -> bool:
    normalized_domain = normalize_domain(allowed_domain)
    if not normalized_domain:
        return False
    return hostname == normalized_domain or hostname.endswith(f".{normalized_domain}")


def _token_prefix_match(left: str, right: str) -> bool:
    min_prefix = 6
    if min(len(left), len(right)) < min_prefix:
        return left == right
    return left[:min_prefix] == right[:min_prefix]


class KnowledgeBase:
    """загружает и опрашивает локальные файлы базы знаний."""

    def __init__(
        self,
        company: CompanyConfig,
        services: list[Service],
        prices: list[PriceEntry],
        faq_markdown: str,
        phrasebook: Optional[dict[str, PhrasebookValue]] = None,
        domain_profile: Optional[dict[str, object]] = None,
        config_payload: Optional[dict[str, object]] = None,
        article_service_map: Optional[dict[str, ArticleServiceMapEntry]] = None,
    ) -> None:
        self.company = company
        self.services = services
        self.prices = prices
        self.faq_markdown = faq_markdown
        self.phrasebook = phrasebook or _default_phrasebook()
        self.domain_profile = domain_profile or _default_domain_profile()
        self.config_payload = config_payload or {}
        self.article_service_map = article_service_map or {}

        self._services_by_id = {service.id: service for service in services}
        self._prices_by_service_id = {price.service_id: price for price in prices}
        self._search_index = self._build_search_index(services)

    @classmethod
    def load(cls, data_dir: Path, defaults_dir: Optional[Path] = None) -> "KnowledgeBase":
        """загружает базу знаний из локальных json/yaml-файлов."""

        company_payload: dict[str, object] = {}
        if defaults_dir is not None:
            defaults_company_path = defaults_dir / "company.yaml"
            if defaults_company_path.exists():
                company_payload.update(yaml.safe_load(defaults_company_path.read_text(encoding="utf-8")) or {})
        company_payload.update(yaml.safe_load((data_dir / "company.yaml").read_text(encoding="utf-8")) or {})
        company = CompanyConfig.model_validate(company_payload)
        services = [
            Service.model_validate(item)
            for item in json.loads((data_dir / "services.json").read_text(encoding="utf-8"))
        ]
        prices = [
            PriceEntry.model_validate(item)
            for item in json.loads((data_dir / "prices.json").read_text(encoding="utf-8"))
        ]
        faq_markdown = (data_dir / "faq.md").read_text(encoding="utf-8")
        config_path = data_dir / "config.yaml"
        config_payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        config_payload = config_payload or {}
        article_service_map = cls._load_article_service_map(data_dir)
        domain_profile = _domain_profile_from_payload(company_payload)
        return cls(
            company=company,
            services=services,
            prices=prices,
            faq_markdown=faq_markdown,
            domain_profile=domain_profile,
            config_payload=config_payload if isinstance(config_payload, dict) else {},
            article_service_map=article_service_map,
        )

    @staticmethod
    def _load_article_service_map(data_dir: Path) -> dict[str, ArticleServiceMapEntry]:
        """читает approved article -> services mapping из папки клиента."""

        map_path = data_dir / "article_service_map.yaml"
        if not map_path.exists():
            return {}

        payload = yaml.safe_load(map_path.read_text(encoding="utf-8")) or {}
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return {}

        approved: dict[str, ArticleServiceMapEntry] = {}
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            entry = ArticleServiceMapEntry.model_validate(raw_item)
            normalized_url = entry.url.strip().rstrip("/")
            if entry.status != "approved" or not normalized_url:
                continue
            approved[normalized_url] = entry
        return approved

    def _build_search_index(self, services: list[Service]) -> dict[str, str]:
        index: dict[str, str] = {}
        for service in services:
            terms = [service.id, service.name, *service.synonyms]
            for term in terms:
                index[normalize_text(term)] = service.id
        return index

    def find_service_by_id(self, service_id: Optional[str]) -> Optional[Service]:
        if not service_id:
            return None
        return self._services_by_id.get(service_id)

    def find_price_by_service_id(self, service_id: Optional[str]) -> Optional[PriceEntry]:
        if not service_id:
            return None
        return self._prices_by_service_id.get(service_id)

    def search_service(self, query: str) -> Optional[Service]:
        normalized_query = normalize_text(query)
        if not normalized_query:
            return None

        direct_match = self._search_index.get(normalized_query)
        if direct_match:
            return self._services_by_id[direct_match]

        for term, service_id in self._search_index.items():
            if term and term in normalized_query:
                return self._services_by_id[service_id]

        query_tokens = _tokens(query)
        for service in self.services:
            variants = [service.name, *service.synonyms]
            for variant in variants:
                variant_tokens = _tokens(variant)
                if not variant_tokens:
                    continue
                if all(
                    any(_token_prefix_match(variant_token, query_token) for query_token in query_tokens)
                    for variant_token in variant_tokens
                ):
                    return service

        return None

    def find_similar_services(self, query: str, threshold: float = 0.6) -> list[Service]:
        normalized_query = normalize_text(query)
        query_tokens = set(_tokens(query))
        if not normalized_query:
            return []

        scored_services: list[tuple[float, Service]] = []
        for service in self.services:
            variants = [service.name, *service.synonyms]
            best_score = 0.0
            for variant in variants:
                normalized_variant = normalize_text(variant)
                variant_tokens = set(_tokens(variant))
                if not normalized_variant:
                    continue

                sequence_score = SequenceMatcher(None, normalized_query, normalized_variant).ratio()
                token_score = 0.0
                if query_tokens and variant_tokens:
                    overlap = query_tokens & variant_tokens
                    prefix_overlap = {
                        query_token
                        for query_token in query_tokens
                        if any(
                            _token_prefix_match(query_token, variant_token)
                            for variant_token in variant_tokens
                        )
                    }
                    token_score = max(
                        len(overlap) / len(variant_tokens),
                        len(prefix_overlap) / len(variant_tokens),
                    )
                    if overlap or prefix_overlap:
                        token_score = max(token_score, 0.62)
                best_score = max(best_score, sequence_score, token_score)

            if best_score >= threshold:
                scored_services.append((best_score, service))

        scored_services.sort(key=lambda item: item[0], reverse=True)
        return [service for _, service in scored_services[:3]]

    def _price_unit_note(self, service: Service) -> str:
        variants = service.variants
        if not variants:
            return ""

        names = [
            str(variant.get("name") or "").strip().lower()
            for variant in variants
            if isinstance(variant, dict) and str(variant.get("name") or "").strip()
        ]
        if not names or len(names) != len(variants):
            return ""

        unit_patterns = (
            re.compile(r"(?:^|[\s(])1\s*ед\.?(?:[\s)]|$)"),
            re.compile(r"(?:^|[\s(])за\s+единиц"),
            re.compile(r"(?:^|[\s(])за\s+мл(?:[\s)]|$)"),
            re.compile(r"(?:^|[\s(])\d+(?:[,.]\d+)?\s*мл(?:[\s)]|$)"),
            re.compile(r"(?:^|[\s(])за\s+укол(?:[\s)]|$)"),
        )
        if all(any(pattern.search(name) for pattern in unit_patterns) for name in names):
            return "Цена указана за единицу препарата или объём; итоговое количество определит менеджер на консультации."

        return ""

    def get_service_context(self, service: Optional[Service]) -> dict[str, object]:
        if service is None:
            return {}

        price = self.find_price_by_service_id(service.id)
        service_payload = service.model_dump()
        price_unit_note = self._price_unit_note(service)
        return {
            "company": {
                "company_name": self.company.company_name,
                "city": self.company.city,
                "working_hours": self.company.working_hours,
                "phone": self.company.phone,
                "address": self.company.address,
            },
            "service": service_payload,
            "price": price.model_dump() if price else None,
            "faq": self.faq_markdown,
            "disclaimer": self.company.safety_disclaimer,
            "phrasebook": self.phrasebook,
            "domain_profile": self.domain_profile,
            "price_unit_note": price_unit_note,
        }


class DuplicateDomainError(Exception):
    """домен совпал с несколькими клиентами."""

    def __init__(self, domain: str, company_ids: list[str]) -> None:
        super().__init__(f"duplicate domain: {domain} matched {company_ids}")
        self.domain = domain
        self.company_ids = company_ids


class KnowledgeBaseResolver:
    """выбирает базу знаний по company_id с fallback для старого demo-flow."""

    def __init__(
        self,
        data_dir: Path,
        clients_data_dir: Path,
        defaults_data_dir: Optional[Path] = None,
        default_company_id: str = "rosh_demo",
    ) -> None:
        self.data_dir = data_dir
        self.clients_data_dir = clients_data_dir
        self.defaults_data_dir = defaults_data_dir
        self.default_company_id = default_company_id
        self._cache: dict[str, KnowledgeBase] = {}
        self._legacy_cache: Optional[KnowledgeBase] = None
        self._domain_index: dict[str, list[str]] = {}
        self._domain_index_built = False

    def _client_dir(self, company_id: str) -> Path:
        clean_company_id = company_id.strip()
        if not CLIENT_ID_PATTERN.fullmatch(clean_company_id):
            raise PermissionError(f"invalid company_id: {company_id!r}")

        clients_root = self.clients_data_dir.resolve()
        client_dir = (clients_root / clean_company_id).resolve()
        if not client_dir.is_relative_to(clients_root):
            raise PermissionError(f"company_id escapes clients root: {company_id!r}")
        return client_dir

    def _legacy_data_exists(self) -> bool:
        return all((self.data_dir / file_name).exists() for file_name in KB_REQUIRED_FILES)

    def _load_legacy(self) -> KnowledgeBase:
        if self._legacy_cache is None:
            self._legacy_cache = KnowledgeBase.load(self.data_dir, self.defaults_data_dir)
        return self._legacy_cache

    def build_domain_index(self) -> dict[str, list[str]]:
        """строит индекс allowed_domains -> company_id для автодетекта."""

        index: dict[str, list[str]] = {}
        clients_root = self.clients_data_dir
        if not clients_root.exists() or not clients_root.is_dir():
            self._domain_index = index
            self._domain_index_built = True
            return index

        for client_dir in clients_root.iterdir():
            if not client_dir.is_dir() or not self.client_exists(client_dir.name):
                continue
            try:
                knowledge_base = self.get(client_dir.name, fallback=False)
            except KeyError:
                continue
            for domain in knowledge_base.company.allowed_domains:
                normalized_domain = normalize_domain(domain)
                if normalized_domain:
                    index.setdefault(normalized_domain, []).append(knowledge_base.company.company_id)

        self._domain_index = {
            domain: sorted(set(company_ids))
            for domain, company_ids in index.items()
        }
        self._domain_index_built = True
        return self._domain_index

    def find_tenant_by_domain(self, origin: str | None) -> str | None:
        """находит company_id по Origin/Referer через allowed_domains клиентов."""

        hostname = hostname_from_origin(origin)
        if not hostname:
            return None
        if not self._domain_index_built:
            self.build_domain_index()

        matched_ids: set[str] = set()
        for domain, company_ids in self._domain_index.items():
            if domain_matches(hostname, domain):
                matched_ids.update(company_ids)

        if len(matched_ids) > 1:
            raise DuplicateDomainError(hostname, sorted(matched_ids))
        if not matched_ids:
            return None
        return next(iter(matched_ids))

    def client_exists(self, company_id: str) -> bool:
        company_id = company_id.strip()
        if not company_id:
            return False
        try:
            client_dir = self._client_dir(company_id)
        except PermissionError:
            return False
        return all((client_dir / file_name).exists() for file_name in KB_REQUIRED_FILES)

    def get(self, company_id: str | None, *, fallback: bool = True) -> KnowledgeBase:
        requested_company_id = (company_id or "").strip()
        if company_id is not None and not requested_company_id and not fallback:
            raise KeyError("")
        target_company_id = requested_company_id or self.default_company_id

        if self.client_exists(target_company_id):
            if target_company_id not in self._cache:
                knowledge_base = KnowledgeBase.load(
                    self._client_dir(target_company_id),
                    self.defaults_data_dir,
                )
                knowledge_base.phrasebook = self.phrasebook(target_company_id)
                knowledge_base.domain_profile = self.domain_profile(target_company_id)
                self._cache[target_company_id] = knowledge_base
            return self._cache[target_company_id]

        if requested_company_id and not CLIENT_ID_PATTERN.fullmatch(requested_company_id):
            raise KeyError(target_company_id)

        if target_company_id == self.default_company_id and self._legacy_data_exists():
            return self._load_legacy()

        if not fallback:
            raise KeyError(target_company_id)

        logger.warning(
            "unknown company_id=%r, falling back to default_company_id=%s",
            requested_company_id,
            self.default_company_id,
        )
        if self.client_exists(self.default_company_id):
            if self.default_company_id not in self._cache:
                knowledge_base = KnowledgeBase.load(self._client_dir(self.default_company_id))
                knowledge_base.phrasebook = self.phrasebook(self.default_company_id)
                knowledge_base.domain_profile = self.domain_profile(self.default_company_id)
                self._cache[self.default_company_id] = knowledge_base
            return self._cache[self.default_company_id]

        return self._load_legacy()

    def widget_features(self, company_id: str) -> dict[str, bool]:
        """возвращает feature-флаги клиента из optional config.yaml."""

        features = dict(DEFAULT_WIDGET_FEATURES)
        payload = self._client_config(company_id)
        raw_features = payload.get("features") if isinstance(payload, dict) else None
        if not isinstance(raw_features, dict):
            return features

        for key in features:
            if key in raw_features:
                features[key] = bool(raw_features[key])
        return features

    def widget_config(self, company_id: str) -> dict[str, object]:
        """возвращает настройки внешнего вида виджета из optional config.yaml."""

        config = dict(DEFAULT_WIDGET_CONFIG)
        payload = self._client_config(company_id)
        raw_widget = payload.get("widget") if isinstance(payload, dict) else None
        if not isinstance(raw_widget, dict):
            return config

        for key in config:
            value = raw_widget.get(key)
            if isinstance(value, str) and value.strip():
                config[key] = value.strip()

        if config["position"] not in {"bottom-right", "bottom-left"}:
            config["position"] = DEFAULT_WIDGET_CONFIG["position"]
        return config

    def domain_profile(self, company_id: str) -> dict[str, object]:
        """возвращает профиль домена для будущей structured intent classification."""

        payload = self._client_config(company_id)
        return _domain_profile_from_payload(payload)

    def phrasebook(self, company_id: str) -> dict[str, PhrasebookValue]:
        """возвращает user-facing фразы клиента с нейтральными fallback-значениями."""

        phrasebook = _default_phrasebook()
        payload = self._client_config(company_id)
        raw_phrasebook = payload.get("phrasebook") if isinstance(payload, dict) else None
        if not isinstance(raw_phrasebook, dict):
            return phrasebook

        for key in phrasebook:
            value = raw_phrasebook.get(key)
            normalized_value = _normalize_phrasebook_value(value)
            if normalized_value is not None:
                phrasebook[key] = normalized_value
        return phrasebook

    def notifications_config(self, company_id: str) -> dict[str, object]:
        """возвращает настройки доставки событий из company.yaml/config.yaml."""

        payload: dict[str, object] = {}
        if not self.client_exists(company_id):
            return payload

        company_path = self._client_dir(company_id) / "company.yaml"
        if company_path.exists() and company_path.is_file():
            company_payload = yaml.safe_load(company_path.read_text(encoding="utf-8")) or {}
            if isinstance(company_payload, dict):
                payload.update(company_payload)

        payload.update(self._client_config(company_id))
        raw_notifications = payload.get("notifications")
        return raw_notifications if isinstance(raw_notifications, dict) else {}

    def _client_config(self, company_id: str) -> dict[str, object]:
        if not self.client_exists(company_id):
            return {}

        config_path = self._client_dir(company_id) / "config.yaml"
        if not config_path.exists() or not config_path.is_file():
            return {}

        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return payload if isinstance(payload, dict) else {}
