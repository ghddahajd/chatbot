"""роуты простой аналитики managed-service mvp."""

from datetime import date, datetime, time, timedelta
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel

from ..auth import verify_operator_token
from ..rate_limit import client_ip


router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# Предохранитель от случайного огромного диапазона (не про производительность на текущих
# объёмах — про то, чтобы опечатка в годе ("2020" вместо "2026") не сканировала годы файлов
# молча). 2026-08-30, кастомный период на дашборде.
MAX_CUSTOM_RANGE_DAYS = 730


def _resolve_date_range(
    days: int, start_date: Optional[date], end_date: Optional[date]
) -> tuple[Optional[datetime], Optional[datetime], str]:
    """Общая логика для /dashboard и /leads — либо пресет `days`, либо явный кастомный
    диапазон start_date/end_date (2026-08-30, оба обязательны вместе — один без другого не
    имеет однозначного смысла). Возвращает (start, end, human_label); start/end оба None в
    пресет-режиме — так методы AnalyticsService принимают `days` как и раньше."""

    if start_date is None and end_date is None:
        label = "всё время" if days >= 3650 else f"{days} дн."
        return None, None, label

    if start_date is None or end_date is None:
        raise HTTPException(status_code=422, detail="start_date и end_date нужны оба вместе")
    if end_date < start_date:
        raise HTTPException(status_code=422, detail="end_date раньше start_date")
    if (end_date - start_date).days > MAX_CUSTOM_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"диапазон длиннее {MAX_CUSTOM_RANGE_DAYS} дней")

    start_dt = datetime.combine(start_date, time.min)
    end_dt = datetime.combine(end_date, time.max)
    label = f"{start_date.strftime('%d.%m.%Y')}–{end_date.strftime('%d.%m.%Y')}"
    return start_dt, end_dt, label


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


@router.get("/chats")
async def analytics_chats(
    request: Request,
    company_id: Optional[str] = Query(default=None),
    scope: str = Query(default="all"),
    limit: int = Query(default=50, ge=1, le=200),
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    """Вкладка "Чаты" (TSK-05) — список диалогов, живые + заархивированные, с фильтром по
    исходу. scope: all | bot_only | operator | lead."""

    verify_operator_token(request, x_operator_token)
    live_sessions = await request.app.state.session_store.list_all()
    conversations = request.app.state.analytics_service.list_conversations(
        live_sessions,
        company_id=company_id,
        scope=scope,
        limit=limit,
    )
    return {"conversations": conversations}


@router.get("/chats/{session_id}")
async def analytics_chat_detail(
    session_id: str,
    request: Request,
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    verify_operator_token(request, x_operator_token)
    live_sessions = await request.app.state.session_store.list_all()
    conversation = request.app.state.analytics_service.get_conversation(session_id, live_sessions)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


@router.get("/dashboard")
async def analytics_dashboard(
    request: Request,
    company_id: Optional[str] = Query(default=None),
    days: int = Query(default=30, ge=1, le=3650),
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    """Всё, что нужно веб-странице аналитики, одним запросом — сводка, операторы, лиды по
    месяцам, топ услуг (с человекочитаемым именем, если company_id известен). `days` — общий
    date-range фильтр дашборда (2026-08-27); leads_by_month намеренно не подчиняется ему —
    это уже своя многомесячная развёртка, отдельный диапазон её только запутает.

    start_date/end_date (2026-08-30) — кастомный период, оба вместе, побеждают `days`, если
    заданы (см. _resolve_date_range)."""

    verify_operator_token(request, x_operator_token)
    analytics_service = request.app.state.analytics_service
    sessions = await request.app.state.session_store.list_all()

    start, end, range_label = _resolve_date_range(days, start_date, end_date)
    custom_range = start is not None and end is not None

    # Воронка джойнит widget_impression/chat_opened/message_answered по session_id — это
    # принципиально теряется, как только событие уходит под ретеншн в rollup (там уже
    # только счётчики, id стёрт), поэтому воронку клампим отдельно, короче ретеншна.
    # activity_by_hour/weekday после сегодняшнего фикса это ограничение больше не касается —
    # они сами дочитывают rollup_file (дата+час, без id) на то, что уже вышло за сырое окно.
    # Для кастомного периода — та же идея, но клэмпим НАЧАЛО (конец остаётся тем, что выбрал
    # пользователь), а не просто "days" — иначе воронка молча съезжает на другие даты.
    funnel_days = min(days, 55)
    trend_days = min(days, 30)
    if custom_range:
        # Живой баг (код-ревью, 2026-08-30): раньше клэмп считался как `end - timedelta(...)`
        # НАПРЯМУЮ от end — а end это datetime.combine(end_date, time.max), т.е. 23:59:59.999999.
        # Клэмпнутый start наследовал это же время суток, оказываясь у самого конца
        # граничного дня — весь этот день выпадал из _within_range почти целиком. Считаем
        # клэмп через ЧИСТУЮ арифметику дат, потом собираем datetime с time.min явно.
        funnel_clamp_date = max(start.date(), end.date() - timedelta(days=54))
        funnel_start, funnel_end = datetime.combine(funnel_clamp_date, time.min), end
        trend_clamp_date = max(start.date(), end.date() - timedelta(days=29))
        trend_start, trend_end = datetime.combine(trend_clamp_date, time.min), end
    else:
        funnel_start = funnel_end = trend_start = trend_end = None

    top_services = [
        {
            "service_id": entry["service_id"],
            "service_name": _resolve_service_name(request, company_id, entry["service_id"]),
            "count": entry["count"],
        }
        for entry in analytics_service.top_services(company_id=company_id, days=days, start=start, end=end)
    ]

    return {
        "company_id": company_id,
        "days": days if not custom_range else None,
        "range_label": range_label,
        "summary": analytics_service.summary(sessions=sessions, company_id=company_id, limit=10),
        "operators": analytics_service.operator_summary(
            company_id=company_id, days=days, start=start, end=end
        )["operators"],
        "leads_by_month": analytics_service.leads_by_month(company_id=company_id),
        "leads_by_reason": analytics_service.leads_by_reason(
            company_id=company_id, days=days, start=start, end=end
        ),
        "top_services": top_services,
        "funnel": analytics_service.conversion_funnel(
            company_id=company_id, days=funnel_days, start=funnel_start, end=funnel_end
        ),
        "unanswered_trend": analytics_service.unanswered_trend(
            company_id=company_id, days=trend_days, start=trend_start, end=trend_end
        ),
        "intent_breakdown": analytics_service.intent_breakdown(
            company_id=company_id, days=days, start=start, end=end
        ),
        "objection_breakdown": analytics_service.objection_breakdown(
            company_id=company_id, days=days, start=start, end=end
        ),
        "top_unanswered_questions": analytics_service.top_unanswered_questions(
            company_id=company_id, days=days, start=start, end=end
        ),
        "top_answered_questions": analytics_service.top_answered_questions(
            company_id=company_id, days=days, start=start, end=end
        ),
        "activity_by_hour": analytics_service.activity_by_hour(
            company_id=company_id, days=days, start=start, end=end
        ),
        "activity_by_weekday": analytics_service.activity_by_weekday(
            company_id=company_id, days=days, start=start, end=end
        ),
        "queue_wait": analytics_service.queue_wait_stats(
            company_id=company_id, days=days, start=start, end=end
        ),
        # period_comparison сравнивает "N дней vs предыдущие N дней ОТ СЕГОДНЯ" — для
        # кастомного периода (не обязательно кончающегося сегодня) это дало бы честно
        # выглядящее, но неверное число. Лучше не показать бейдж дельты вообще, чем соврать.
        "period_comparison": (
            analytics_service.period_comparison(company_id=company_id, days=days) if not custom_range else None
        ),
    }


@router.get("/leads")
async def analytics_leads(
    request: Request,
    company_id: Optional[str] = Query(default=None),
    days: int = Query(default=30, ge=1, le=3650),
    start_date: Optional[date] = Query(default=None),
    end_date: Optional[date] = Query(default=None),
    reason: Optional[str] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    x_operator_token: Optional[str] = Header(default=None),
) -> dict:
    """Таблица лидов, "уровень 0" (2026-08-30) — построчный список БЕЗ персональных данных
    (см. AnalyticsService.leads_feed: name/phone/summary/recent_messages физически не читаются
    и не отдаются, это не сокрытие на фронте). service_id резолвится в человекочитаемое имя
    так же, как в /dashboard's top_services."""

    verify_operator_token(request, x_operator_token)
    analytics_service = request.app.state.analytics_service
    start, end, range_label = _resolve_date_range(days, start_date, end_date)

    leads = analytics_service.leads_feed(
        company_id=company_id, days=days, start=start, end=end, reason=reason, limit=limit
    )
    for lead in leads:
        service_id = lead.get("service_id")
        lead["service_name"] = _resolve_service_name(request, company_id, service_id) if service_id else None

    return {"company_id": company_id, "range_label": range_label, "leads": leads}
