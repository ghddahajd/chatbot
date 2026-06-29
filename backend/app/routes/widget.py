"""роуты публичной настройки виджета."""

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ..knowledge import DuplicateDomainError, domain_matches, hostname_from_origin
from ..models import WidgetBootstrapResponse


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/widget", tags=["widget"])


def check_origin(origin: str | None, company_domains: list[str], dev_mode: bool) -> bool:
    if not origin:
        return dev_mode

    hostname = hostname_from_origin(origin)
    if not hostname:
        return False

    if dev_mode and hostname in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return True

    return any(domain_matches(hostname, domain) for domain in company_domains)


@router.get("/bootstrap", response_model=WidgetBootstrapResponse)
async def bootstrap_widget(
    request: Request,
    company_id: Optional[str] = Query(default=None, min_length=1),
    x_company_id: Optional[str] = Header(default=None),
) -> WidgetBootstrapResponse:
    resolver = request.app.state.knowledge_base_resolver
    origin = request.headers.get("origin") or request.headers.get("referer")
    resolved_company_id = company_id or x_company_id

    if not resolved_company_id:
        try:
            resolved_company_id = resolver.find_tenant_by_domain(origin)
        except DuplicateDomainError as error:
            logger.warning("duplicate domain: %s matched %s", error.domain, error.company_ids)
            raise HTTPException(status_code=409, detail="Duplicate domain configuration") from error
        if not resolved_company_id:
            raise HTTPException(status_code=404, detail="Unknown company")

    try:
        knowledge_base = resolver.get(resolved_company_id, fallback=False)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown company") from error

    company = knowledge_base.company
    if not check_origin(origin, company.allowed_domains, request.app.state.settings.dev_mode):
        raise HTTPException(status_code=403, detail="Domain not allowed")

    return WidgetBootstrapResponse(
        company_id=company.company_id,
        company_name=company.company_name,
        city=company.city,
        website_url=company.website_url,
        telegram_url=company.telegram_url,
        features=resolver.widget_features(company.company_id),
    )
