"""Постановка операторского события без подавления отказа SQL-хранилища."""

import logging

from ..storage.errors import LeadStorageError

logger = logging.getLogger(__name__)


async def operator_event_failed(request, *, company_id, session_id, message, operator_url) -> bool:
    delivery = getattr(request.app.state, "delivery_service", None)
    if delivery is None:
        return False
    try:
        await delivery.enqueue_event(
            event_type="operator_requested", company_id=company_id, session_id=session_id,
            payload={"last_message": message, "operator_url": operator_url},
        )
    except Exception as error:
        logger.warning("operator delivery enqueue failed company_id=%s session_id=%s error=%s",
                       company_id, session_id, type(error).__name__)
        return isinstance(error, LeadStorageError)
    return False
