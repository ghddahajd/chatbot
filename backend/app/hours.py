"""проверка "открыта ли компания прямо сейчас" по структурированному расписанию."""

from __future__ import annotations

from datetime import datetime, time
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import DaySchedule

_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DEFAULT_TIMEZONE = "Europe/Moscow"


def _resolve_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name or _DEFAULT_TIMEZONE)
    except (ZoneInfoNotFoundError, ValueError):
        # Битое имя таймзоны в конфиге клиента не должно ронять весь пайплайн — откатываемся
        # на дефолт и продолжаем работать, а не 500-им на ровном месте.
        return ZoneInfo(_DEFAULT_TIMEZONE)


def _parse_time(value: str) -> Optional[time]:
    try:
        hours, minutes = value.strip().split(":")
        return time(int(hours), int(minutes))
    except (ValueError, AttributeError):
        return None


def is_currently_open(
    schedule: dict[str, Optional[DaySchedule]],
    timezone_name: str,
    now: Optional[datetime] = None,
) -> bool:
    """Открыта ли компания прямо сейчас по расписанию.

    Пустое расписание (клиент его не настроил) — считаем открытым всегда, это дефолт,
    сохраняющий текущее поведение для всех клиентов без этого поля. `now` — опционально:
    тесты передают конкретное время, прод оставляет None и берёт реальные часы. Не
    поддерживает смену через полночь (close < open) — сегодняшним клиентам это не нужно,
    не усложняем раньше времени.
    """

    if not schedule:
        return True

    tz = _resolve_timezone(timezone_name)
    current = now if now is not None else datetime.now(tz)
    current = current.astimezone(tz) if current.tzinfo is not None else current.replace(tzinfo=tz)

    day_schedule = schedule.get(_WEEKDAY_KEYS[current.weekday()])
    if day_schedule is None:
        return False

    open_time = _parse_time(day_schedule.open)
    close_time = _parse_time(day_schedule.close)
    if open_time is None or close_time is None:
        # Некорректно заполненное расписание — не блокируем реальных клиентов из-за опечатки
        # в конфиге, ведём себя как будто расписания нет.
        return True

    return open_time <= current.time() < close_time
