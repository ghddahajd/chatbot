"""роуты публичной настройки виджета."""

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request

from ..models import CompanyConfig, WidgetBootstrapResponse


router = APIRouter(prefix="/api/widget", tags=["widget"])


def _hostname_from_origin(value: str | None) -> str | None:
    if not value:
        return None

    parsed = urlparse(value)
    if parsed.hostname:
        return parsed.hostname.lower()

    parsed = urlparse(f"//{value}")
    if parsed.hostname:
        return parsed.hostname.lower()

    return value.split("/", 1)[0].split(":", 1)[0].lower() or None


def _normalize_domain(value: str) -> str:
    return (_hostname_from_origin(value) or value).strip().lower().rstrip(".")


def _is_domain_allowed(hostname: str, allowed_domain: str) -> bool:
    normalized_domain = _normalize_domain(allowed_domain)
    if not normalized_domain:
        return False
    return hostname == normalized_domain or hostname.endswith(f".{normalized_domain}")


def check_origin(origin: str | None, company: CompanyConfig, dev_mode: bool) -> bool:
    if not origin:
        return dev_mode

    hostname = _hostname_from_origin(origin)
    if not hostname:
        return False

    if dev_mode and hostname in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return True

    return any(_is_domain_allowed(hostname, domain) for domain in company.allowed_domains)


@router.get("/bootstrap", response_model=WidgetBootstrapResponse)
async def bootstrap_widget(
    request: Request,
    company_id: str = Query(..., min_length=1),
) -> WidgetBootstrapResponse:
    resolver = request.app.state.knowledge_base_resolver
    try:
        knowledge_base = resolver.get(company_id, fallback=False)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown company") from error

    company = knowledge_base.company
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not check_origin(origin, company, request.app.state.settings.dev_mode):
        raise HTTPException(status_code=403, detail="Domain not allowed")

    return WidgetBootstrapResponse(
        company_id=company.company_id,
        company_name=company.company_name,
        city=company.city,
        website_url=company.website_url,
        telegram_url=company.telegram_url,
    )
