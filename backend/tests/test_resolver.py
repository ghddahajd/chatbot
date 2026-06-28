"""проверки KnowledgeBaseResolver."""

import pytest

from app.knowledge import DuplicateDomainError


def test_known_company_loads_kb(resolver) -> None:
    knowledge_base = resolver.get("rosh_demo", fallback=False)

    assert knowledge_base.company.company_id == "rosh_demo"
    assert knowledge_base.services


def test_unknown_company_raises_without_fallback(resolver) -> None:
    with pytest.raises(KeyError):
        resolver.get("missing_company", fallback=False)


def test_path_traversal_blocked(resolver) -> None:
    assert resolver.client_exists("../../etc/passwd") is False
    with pytest.raises(KeyError):
        resolver.get("../../etc/passwd", fallback=False)


def test_domain_autodetect_known(resolver) -> None:
    assert resolver.find_tenant_by_domain("http://localhost:5500") == "rosh_demo"


def test_domain_autodetect_unknown_returns_none(resolver) -> None:
    assert resolver.find_tenant_by_domain("https://unknown.example") is None


def test_duplicate_domain_raises_409(resolver) -> None:
    with pytest.raises(DuplicateDomainError):
        resolver.find_tenant_by_domain("https://duplicate.example")
