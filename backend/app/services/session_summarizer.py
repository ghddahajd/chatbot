"""LLM-саммари диалога для лида с жёстким timeout и детерминированным fallback."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..models import Lead, Session
from ..policy.constants import PHONE_PATTERN
from ..policy.extractors import extract_phone


logger = logging.getLogger(__name__)
DEFAULT_TIMEOUT_SECONDS = 2.5


def _normalize_phones_in_text(text: str) -> str:
    """Саммари цитирует номер ровно как клиент его напечатал ("903 5175776") — LLM не
    получает lead.phone в промпте вообще, она просто копирует цифры из истории переписки,
    поэтому просить её через промпт писать в формате +7 ненадёжно (зависит от того,
    послушается ли модель). Прогоняем готовый текст через тот же extract_phone(), что уже
    решает формат для поля "Телефон:" в карточке — тогда оба места гарантированно совпадают,
    независимо от LLM. re.Match, не найдено — оставляем как есть, не роняем саммари."""

    def _replace(match: Any) -> str:
        return extract_phone(match.group(0)) or match.group(0)

    return PHONE_PATTERN.sub(_replace, text)


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

    cleaned = summary.strip() if isinstance(summary, str) else ""
    return _normalize_phones_in_text(cleaned) if cleaned else fallback
