"""Opt-in wiring; SQL credentials and errors never cross the runtime boundary."""

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar

from .errors import LeadStorageError
from .request_receipts import MemoryRequestReceipts


LEADS_SNAPSHOT = ContextVar("postgres_leads_snapshot", default=None)


@asynccontextmanager
async def leads_snapshot(service, company_id=None):
    """Load selected tenant history once; aggregates still run in Python, not SQL."""
    if service.postgres_lead_store is None:
        yield
        return
    try:
        rows = await service.postgres_lead_store.read_leads(company_id=company_id)
    except Exception:
        raise LeadStorageError("Lead storage unavailable") from None
    token = LEADS_SNAPSHOT.set((service, rows))
    try:
        yield
    finally:
        LEADS_SNAPSHOT.reset(token)


async def initialize_lead_storage(app):
    settings = app.state.settings
    app.state.postgres_lead_store = None
    app.state.request_receipts = MemoryRequestReceipts()
    if settings.lead_storage_backend != "postgres":
        return
    if not settings.delivery_retry_enabled:
        raise LeadStorageError("Postgres lead storage requires delivery retries")
    store = None
    try:
        from ..db.connection import make_engine
        from ..services.postgres_delivery import PostgresDeliveryService, PostgresLeadService
        from .postgres import PostgresLeadOutboxStore

        store = PostgresLeadOutboxStore(make_engine("LEAD_"))
        await store.initialize()
        await store.validate_companies(app.state.knowledge_base_resolver.company_ids())
        delivery = PostgresDeliveryService(
            store=store,
            knowledge_base_resolver=app.state.knowledge_base_resolver,
            telegram_bot_token=settings.telegram_bot_token,
            telegram_chat_id=settings.telegram_chat_id,
            telegram_dm_enabled=settings.telegram_dm_enabled,
        )
        app.state.lead_service = PostgresLeadService(store=store, delivery_service=delivery)
        app.state.delivery_service = delivery
        app.state.postgres_lead_store = store
        from .postgres_receipts import PostgresRequestReceipts
        app.state.request_receipts = PostgresRequestReceipts(store)
    except BaseException as error:
        if store is not None:
            try:
                await store.close()
            except Exception:
                pass
        if not isinstance(error, Exception):
            raise
        raise LeadStorageError("Lead storage unavailable") from None


async def delivery_health(app):
    try:
        return await app.state.delivery_service.get_health()
    except Exception:
        return {"status": "error", "detail": "Lead delivery storage unavailable"}


async def close_lead_storage(app):
    store = getattr(app.state, "postgres_lead_store", None)
    if store is None:
        return
    # A failed worker must not skip stopping the remaining users of this engine.
    tasks = list(getattr(app.state, "background_tasks", {}).values())
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    try:
        await store.close()
    except Exception:
        logging.getLogger(__name__).warning("lead storage close failed")


async def lead_storage_error_handler(request, error):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=503, content={"detail": "Lead storage unavailable"})
