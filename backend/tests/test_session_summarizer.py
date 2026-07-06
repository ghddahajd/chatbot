"""проверки session_summarizer: timeout+fallback, mock не выдумывает факты."""

import asyncio

import pytest

from app.llm.mock import MockLLMClient
from app.models import Lead, Message, MessageRole, Session
from app.services.session_summarizer import summarize_session


def _session_with_messages(*texts: str) -> Session:
    session = Session(company_id="rosh_demo")
    for text in texts:
        session.messages.append(Message(role=MessageRole.USER, text=text))
    return session


def _lead(summary: str, reason: str = "commercial_interest") -> Lead:
    return Lead(
        company_id="rosh_demo",
        session_id="s1",
        name="Иван",
        phone="+79991234567",
        summary=summary,
        reason=reason,
    )


class _SlowLLMClient:
    async def summarize_session(self, session, lead):
        await asyncio.sleep(10)
        return "не должно вернуться"


class _BrokenLLMClient:
    async def summarize_session(self, session, lead):
        raise RuntimeError("boom")


@pytest.mark.anyio
async def test_summarize_session_falls_back_on_timeout() -> None:
    session = _session_with_messages("хочу татуаж")
    lead = _lead("хочу татуаж | Контакт: Иван +79991234567")

    result = await summarize_session(_SlowLLMClient(), session=session, lead=lead, timeout_seconds=0.05)

    assert result == lead.summary


@pytest.mark.anyio
async def test_summarize_session_falls_back_on_exception() -> None:
    session = _session_with_messages("хочу татуаж")
    lead = _lead("хочу татуаж | Контакт: Иван +79991234567")

    result = await summarize_session(_BrokenLLMClient(), session=session, lead=lead)

    assert result == lead.summary


@pytest.mark.anyio
async def test_mock_summarize_session_keeps_original_text_no_invention() -> None:
    session = _session_with_messages("хочу чистку лица", "Иван +79991234567")
    lead = _lead("хочу чистку лица | Контакт: Иван +79991234567", reason="commercial_interest")

    result = await summarize_session(MockLLMClient(), session=session, lead=lead)

    assert "чистку лица" in result
    assert "Иван +79991234567" in result
