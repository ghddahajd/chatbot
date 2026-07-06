"""LLM-саммари диалога для лида с жёстким timeout и детерминированным fallback."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..models import Lead, Session


logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SECONDS = 2.5


async def summarize_session(
    llm_client: Any,
    *,
    session: Session,
    lead: Lead,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """возвращает summary для лида; при таймауте/ошибке не теряет уже построенный fallback."""

    fallback = lead.summary
    try:
        summary = await asyncio.wait_for(
            llm_client.summarize_session(session, lead),
            timeout=timeout_seconds,
        )
    except Exception as error:
        logger.info("session_summary_source=fallback reason=%s", type(error).__name__)
        return fallback

    return summary.strip() if isinstance(summary, str) and summary.strip() else fallback
