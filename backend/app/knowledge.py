"""загрузка базы знаний и вспомогательные функции для поиска."""

from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import yaml

from .models import CompanyConfig, PriceEntry, Service


logger = logging.getLogger(__name__)
CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
KB_REQUIRED_FILES = ("company.yaml", "services.json", "prices.json", "faq.md")
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
    "price_disclaimer": "Предварительно так, точнее подскажет менеджер после уточнения деталей.",
    "unknown_service": "Такой услуги у нас пока не вижу — показать список услуг или подключить менеджера?",
    "off_topic": "Это не по моей части — я консультирую по услугам компании. Могу подсказать по услугам или ценам.",
    "contact_prompt": "Оставьте имя и телефон, и менеджер сможет связаться с вами позже.",
    "booking_contact_prompt": "Чтобы оставить заявку, напишите имя, телефон и удобное время. Мы передадим заявку, а менеджер подтвердит детали.",
    "handoff_message": "Передаю диалог менеджеру. Можете дописать детали, он увидит историю.",
    "operator_soft_offer": "Могу попробовать помочь здесь — опишите, что вас интересует. Или сразу соединю с менеджером.",
    "clarify": "Уточните, пожалуйста, что вас интересует? Могу рассказать про услуги, цены или оформить заявку.",
    "empty_message": "Похоже, сообщение пустое. Напишите вопрос, и я подскажу.",
    "empty_message_letters": "Не совсем понял вопрос. Можете переформулировать словами?",
    "rate_limit": "Похоже, разговор затянулся. Если остались вопросы — оставьте телефон, мы свяжемся, или попробуйте начать новый диалог.",
    "human_active_wait": "Чат передан менеджеру. Пожалуйста, дождитесь ответа.",
    "contact_cancelled": "Ок, контакт не оставляем. Могу подсказать по услугам, ценам или позвать менеджера.",
    "booking_cancelled": "Ок, заявку не оформляем. Могу подсказать по услугам, ценам или позвать менеджера.",
    "lead_success": "Спасибо. Передали ваши контакты менеджеру. С вами свяжутся для уточнения деталей.",
    "booking_success": "Спасибо. Заявку передали. С вами свяжутся, чтобы подтвердить время и детали.",
}


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
    min_prefix = 5
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
        phrasebook: Optional[dict[str, str]] = None,
        domain_profile: Optional[dict[str, object]] = None,
        config_payload: Optional[dict[str, object]] = None,
    ) -> None:
        self.company = company
        self.services = services
        self.prices = prices
        self.faq_markdown = faq_markdown
        self.phrasebook = phrasebook or dict(DEFAULT_PHRASEBOOK)
        self.domain_profile = domain_profile or _default_domain_profile()
        self.config_payload = config_payload or {}

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
        domain_profile = _domain_profile_from_payload(company_payload)
        return cls(
            company=company,
            services=services,
            prices=prices,
            faq_markdown=faq_markdown,
            domain_profile=domain_profile,
            config_payload=config_payload if isinstance(config_payload, dict) else {},
        )

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

    def phrasebook(self, company_id: str) -> dict[str, str]:
        """возвращает user-facing фразы клиента с нейтральными fallback-значениями."""

        phrasebook = dict(DEFAULT_PHRASEBOOK)
        payload = self._client_config(company_id)
        raw_phrasebook = payload.get("phrasebook") if isinstance(payload, dict) else None
        if not isinstance(raw_phrasebook, dict):
            return phrasebook

        for key in phrasebook:
            value = raw_phrasebook.get(key)
            if isinstance(value, str) and value.strip():
                phrasebook[key] = value.strip()
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
