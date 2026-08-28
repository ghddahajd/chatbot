"""is_currently_open — чистая функция проверки рабочих часов (2026-08-28, TSK-02)."""

from datetime import datetime
from zoneinfo import ZoneInfo

from app.hours import is_currently_open
from app.models import DaySchedule

_MOSCOW_SCHEDULE = {
    "mon": DaySchedule(open="10:00", close="21:00"),
    "tue": DaySchedule(open="10:00", close="21:00"),
    "wed": DaySchedule(open="10:00", close="21:00"),
    "thu": DaySchedule(open="10:00", close="21:00"),
    "fri": DaySchedule(open="10:00", close="21:00"),
    "sat": DaySchedule(open="10:00", close="21:00"),
    "sun": DaySchedule(open="10:00", close="21:00"),
}


def _moscow(year, month, day, hour, minute) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("Europe/Moscow"))


def test_open_during_business_hours() -> None:
    # 2026-08-28 — пятница, 15:00 МСК
    now = _moscow(2026, 8, 28, 15, 0)
    assert is_currently_open(_MOSCOW_SCHEDULE, "Europe/Moscow", now=now) is True


def test_closed_before_opening() -> None:
    now = _moscow(2026, 8, 28, 3, 0)
    assert is_currently_open(_MOSCOW_SCHEDULE, "Europe/Moscow", now=now) is False


def test_closed_after_closing() -> None:
    now = _moscow(2026, 8, 28, 23, 0)
    assert is_currently_open(_MOSCOW_SCHEDULE, "Europe/Moscow", now=now) is False


def test_open_at_exact_opening_boundary() -> None:
    now = _moscow(2026, 8, 28, 10, 0)
    assert is_currently_open(_MOSCOW_SCHEDULE, "Europe/Moscow", now=now) is True


def test_closed_at_exact_closing_boundary() -> None:
    now = _moscow(2026, 8, 28, 21, 0)
    assert is_currently_open(_MOSCOW_SCHEDULE, "Europe/Moscow", now=now) is False


def test_closed_on_a_day_marked_none() -> None:
    schedule = dict(_MOSCOW_SCHEDULE)
    schedule["sun"] = None
    # 2026-08-30 — воскресенье
    now = _moscow(2026, 8, 30, 15, 0)
    assert is_currently_open(schedule, "Europe/Moscow", now=now) is False


def test_empty_schedule_is_always_open() -> None:
    """Клиент не настроил расписание — не меняем поведение, всегда открыто (дефолт для всех
    существующих клиентов, у которых этого поля просто нет в конфиге)."""

    now = _moscow(2026, 8, 28, 3, 0)
    assert is_currently_open({}, "Europe/Moscow", now=now) is True


def test_malformed_day_schedule_fails_open_instead_of_crashing() -> None:
    schedule = {"fri": DaySchedule(open="not-a-time", close="21:00")}
    now = _moscow(2026, 8, 28, 3, 0)
    assert is_currently_open(schedule, "Europe/Moscow", now=now) is True


def test_unknown_timezone_falls_back_to_moscow_instead_of_crashing() -> None:
    now = _moscow(2026, 8, 28, 15, 0)
    assert is_currently_open(_MOSCOW_SCHEDULE, "Not/A_Real_Zone", now=now) is True


def test_naive_now_is_treated_as_the_configured_timezone() -> None:
    naive_now = datetime(2026, 8, 28, 15, 0)  # без tzinfo
    assert is_currently_open(_MOSCOW_SCHEDULE, "Europe/Moscow", now=naive_now) is True
