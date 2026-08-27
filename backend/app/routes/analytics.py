"""роуты простой аналитики managed-service mvp."""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from ..auth import verify_operator_token
from ..rate_limit import client_ip


router = APIRouter(prefix="/api/analytics", tags=["analytics"])


class TrackEventRequest(BaseModel):
    company_id: str
    session_id: str = ""


def _check_track_rate_limit(request: Request) -> None:
    # Публичный, неавторизованный эндпоинт (зовёт браузер любого посетителя сайта, не
    # оператор) — переиспользуем тот же лимитер, что и /api/chat/message, чтобы не плодить
    # отдельный механизм под лёгкие "маячки" воронки.
    settings = request.app.state.settings
    if not settings.chat_rate_limit_enabled:
        return
    limiter = request.app.state.chat_rate_limiter
    if not limiter.allow(client_ip(request, trusted_proxy_count=settings.trusted_proxy_count)):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait.")


@router.post("/track/impression")
async def track_widget_impression(payload: TrackEventRequest, request: Request) -> dict:
    """Виджет вызывает при монтировании на странице — верхняя стадия воронки
    ("Виджет загружен"), см. AnalyticsService.conversion_funnel."""

    _check_track_rate_limit(request)
    await request.app.state.analytics_service.track_event(
        company_id=payload.company_id,
        session_id=payload.session_id,
        event_type="widget_impression",
    )
    return {"ok": True}


@router.post("/track/chat-opened")
async def track_chat_opened(payload: TrackEventRequest, request: Request) -> dict:
    """Виджет вызывает при первом открытии панели чата за загрузку страницы (см. widget.js
    toggle()) — вторая стадия воронки ("Чат открыт")."""

    _check_track_rate_limit(request)
    await request.app.state.analytics_service.track_event(
        company_id=payload.company_id,
        session_id=payload.session_id,
        event_type="chat_opened",
    )
    return {"ok": True}


@router.get("/summary")
async def analytics_summary(
    request: Request,
    company_id: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    verify_operator_token(request, x_operator_token)
    sessions = await request.app.state.session_store.list_all()
    return request.app.state.analytics_service.summary(
        sessions=sessions,
        company_id=company_id,
        limit=limit,
    )


@router.get("/operators")
async def analytics_operators(
    request: Request,
    company_id: Optional[str] = Query(default=None),
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    verify_operator_token(request, x_operator_token)
    return request.app.state.analytics_service.operator_summary(company_id=company_id)


def _resolve_service_name(request: Request, company_id: Optional[str], service_id: str) -> str:
    if not company_id:
        return service_id
    try:
        knowledge_base = request.app.state.knowledge_base_resolver.get(company_id, fallback=False)
    except KeyError:
        return service_id
    service = knowledge_base.find_service_by_id(service_id)
    return service.name if service is not None else service_id


@router.get("/dashboard")
async def analytics_dashboard(
    request: Request,
    company_id: Optional[str] = Query(default=None),
    days: int = Query(default=30, ge=1, le=3650),
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    """Всё, что нужно веб-странице аналитики, одним запросом — сводка, операторы, лиды по
    месяцам, топ услуг (с человекочитаемым именем, если company_id известен). `days` — общий
    date-range фильтр дашборда (2026-08-27); leads_by_month намеренно не подчиняется ему —
    это уже своя многомесячная развёртка, отдельный диапазон её только запутает."""

    verify_operator_token(request, x_operator_token)
    analytics_service = request.app.state.analytics_service
    sessions = await request.app.state.session_store.list_all()

    # Воронка джойнит widget_impression/chat_opened/message_answered по session_id — это
    # принципиально теряется, как только событие уходит под ретеншн в rollup (там уже
    # только счётчики, id стёрт), поэтому воронку клампим отдельно, короче ретеншна.
    # activity_by_hour/weekday после сегодняшнего фикса это ограничение больше не касается —
    # они сами дочитывают rollup_file (дата+час, без id) на то, что уже вышло за сырое окно.
    funnel_days = min(days, 55)

    top_services = [
        {
            "service_id": entry["service_id"],
            "service_name": _resolve_service_name(request, company_id, entry["service_id"]),
            "count": entry["count"],
        }
        for entry in analytics_service.top_services(company_id=company_id, days=days)
    ]

    return {
        "company_id": company_id,
        "days": days,
        "summary": analytics_service.summary(sessions=sessions, company_id=company_id, limit=10),
        "operators": analytics_service.operator_summary(company_id=company_id, days=days)["operators"],
        "leads_by_month": analytics_service.leads_by_month(company_id=company_id),
        "leads_by_reason": analytics_service.leads_by_reason(company_id=company_id, days=days),
        "top_services": top_services,
        "funnel": analytics_service.conversion_funnel(company_id=company_id, days=funnel_days),
        "unanswered_trend": analytics_service.unanswered_trend(company_id=company_id, days=min(days, 30)),
        "activity_by_hour": analytics_service.activity_by_hour(company_id=company_id, days=days),
        "activity_by_weekday": analytics_service.activity_by_weekday(company_id=company_id, days=days),
        "queue_wait": analytics_service.queue_wait_stats(company_id=company_id, days=days),
        "period_comparison": analytics_service.period_comparison(company_id=company_id, days=days),
    }
