"""карточка «Быстрая запись» в виджете: день и номер в одной карточке вместо двух шагов переписки."""

from __future__ import annotations

from ..hours import is_currently_open
from ..knowledge import normalize_text
from ..models import ChatMessageResponse, PendingAction, Session, SessionStatus
from ..policy.constants import BOOKING_CONTACT_ALTERNATIVES, BOOKING_DAY_CHOICES, BOOKING_WHEN_TILES
from ..routes.chat_utils import format_quick_actions

PREFERRED_TIME_KEY = "preferred_time"


def day_preference(day_choice: tuple[str, str], company) -> tuple[str, str]:
    """«Когда удобно» для карточки администратору и ключ ответного текста."""

    preference, phrase_key = day_choice
    if phrase_key == "booking_when_today" and not is_currently_open(company.working_hours_schedule, company.timezone):
        # администратор прочитает карточку утром — «сегодня» без пометки было бы двусмысленным
        return "сегодня — запрос пришёл в нерабочее время", "booking_when_today_closed"
    return preference, phrase_key


async def remember_card_day(session_store, session: Session, booking_day: str, company) -> Session:
    """день из карточки пришёл вместе с номером — в «Когда удобно» его кладём до того, как соберётся заявка."""

    day_choice = BOOKING_DAY_CHOICES.get(normalize_text(booking_day))
    if day_choice is None or session.pending_action != PendingAction.BOOKING_CONTACT.value:
        return session
    preference, _ = day_preference(day_choice, company)
    await session_store.update_contact_draft(session.session_id, metadata={PREFERRED_TIME_KEY: preference})
    return await session_store.get(session.session_id) or session


async def attach_form(request, company_id: str, session_id: str, response) -> None:
    """помечает ответ карточкой, если бот ждёт номер для новой записи, а в «Настройках» она не выключена."""

    if not isinstance(response, ChatMessageResponse) or response.lead_created or response.status != SessionStatus.AI_ACTIVE:
        return
    session = await request.app.state.session_store.get(session_id)
    if session is None or session.pending_action != PendingAction.BOOKING_CONTACT.value:
        return  # настройки клиента читаются с диска — только когда карточка вообще возможна
    resolver = request.app.state.knowledge_base_resolver
    if resolver.widget_config(company_id).get("quick_booking") != "on":
        return
    knowledge_base = resolver.get(company_id, fallback=False)
    company = knowledge_base.company
    day_known = bool(str(session.contact_draft.get(PREFERRED_TIME_KEY) or "").strip())
    # отдельного шага «оставьте номер» с этими кнопками в карточке нет — кто не хочет давать номер, звонит сам
    contacts = format_quick_actions(list(BOOKING_CONTACT_ALTERNATIVES), request, knowledge_base)
    response.booking_form = {
        "days": [] if day_known else list(BOOKING_WHEN_TILES),
        "open_now": is_currently_open(company.working_hours_schedule, company.timezone),
        "contacts": [action.model_dump() for action in contacts],
    }
