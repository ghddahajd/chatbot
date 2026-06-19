"""Knowledge base loading and lookup helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

import yaml

from .models import CompanyConfig, PriceEntry, Service


def normalize_text(value: str) -> str:
    """Normalize text for fuzzy keyword matching."""

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
    """Loads and queries local knowledge-base files."""

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
        """Load the knowledge base from local JSON/YAML files."""

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
