"""роуты публичной настройки виджета."""

from fastapi import APIRouter, HTTPException, Query, Request

from ..models import WidgetBootstrapResponse


router = APIRouter(prefix="/api/widget", tags=["widget"])


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
    return WidgetBootstrapResponse(
        company_id=company.company_id,
        company_name=company.company_name,
        city=company.city,
        website_url=company.website_url,
        telegram_url=company.telegram_url,
    )
