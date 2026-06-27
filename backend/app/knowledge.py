"""загрузка базы знаний и вспомогательные функции для поиска."""

from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import yaml

from .models import CompanyConfig, PriceEntry, Service


logger = logging.getLogger(__name__)


def normalize_text(value: str) -> str:
    """нормализует текст для нечёткого поиска по ключевым словам."""

    normalized = value.lower().replace("ё", "е")
    normalized = re.sub(r"[^a-zа-я0-9\s]+", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def _tokens(value: str) -> list[str]:
    return [token for token in normalize_text(value).split() if token]


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
    ) -> None:
        self.company = company
        self.services = services
        self.prices = prices
        self.faq_markdown = faq_markdown

        self._services_by_id = {service.id: service for service in services}
        self._prices_by_service_id = {price.service_id: price for price in prices}
        self._search_index = self._build_search_index(services)

    @classmethod
    def load(cls, data_dir: Path) -> "KnowledgeBase":
        """загружает базу знаний из локальных json/yaml-файлов."""

        company = CompanyConfig.model_validate(
            yaml.safe_load((data_dir / "company.yaml").read_text(encoding="utf-8"))
        )
        services = [
            Service.model_validate(item)
            for item in json.loads((data_dir / "services.json").read_text(encoding="utf-8"))
        ]
        prices = [
            PriceEntry.model_validate(item)
            for item in json.loads((data_dir / "prices.json").read_text(encoding="utf-8"))
        ]
        faq_markdown = (data_dir / "faq.md").read_text(encoding="utf-8")
        return cls(company=company, services=services, prices=prices, faq_markdown=faq_markdown)

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

    def get_service_context(self, service: Optional[Service]) -> dict[str, object]:
        if service is None:
            return {}

        price = self.find_price_by_service_id(service.id)
        service_payload = service.model_dump()
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
            "disclaimer": self.company.medical_disclaimer,
        }


class KnowledgeBaseResolver:
    """выбирает базу знаний по company_id с fallback для старого demo-flow."""

    def __init__(
        self,
        data_dir: Path,
        clients_data_dir: Path,
        default_company_id: str = "rosh_demo",
    ) -> None:
        self.data_dir = data_dir
        self.clients_data_dir = clients_data_dir
        self.default_company_id = default_company_id
        self._cache: dict[str, KnowledgeBase] = {}
        self._legacy_cache: Optional[KnowledgeBase] = None

    def _client_dir(self, company_id: str) -> Path:
        return self.clients_data_dir / company_id

    def _legacy_data_exists(self) -> bool:
        return all(
            (self.data_dir / file_name).exists()
            for file_name in ("company.yaml", "services.json", "prices.json", "faq.md")
        )

    def _load_legacy(self) -> KnowledgeBase:
        if self._legacy_cache is None:
            self._legacy_cache = KnowledgeBase.load(self.data_dir)
        return self._legacy_cache

    def client_exists(self, company_id: str) -> bool:
        company_id = company_id.strip()
        if not company_id:
            return False
        client_dir = self._client_dir(company_id)
        return all(
            (client_dir / file_name).exists()
            for file_name in ("company.yaml", "services.json", "prices.json", "faq.md")
        )

    def get(self, company_id: str | None, *, fallback: bool = True) -> KnowledgeBase:
        requested_company_id = (company_id or "").strip()
        target_company_id = requested_company_id or self.default_company_id

        if self.client_exists(target_company_id):
            if target_company_id not in self._cache:
                self._cache[target_company_id] = KnowledgeBase.load(self._client_dir(target_company_id))
            return self._cache[target_company_id]

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
                self._cache[self.default_company_id] = KnowledgeBase.load(self._client_dir(self.default_company_id))
            return self._cache[self.default_company_id]

        return self._load_legacy()
