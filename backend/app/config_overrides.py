"""Локальные оверрайды базовых настроек клиента ("Настройки"-таб, TSK-06, 2026-08-29).

Живёт СНАРУЖИ backend/data/clients/<id>/ — не потому что "так проще", а потому что
docker-entrypoint.sh на каждом деплое делает `rm -rf` ИМЕННО на весь /app/data/clients/<id>
перед тем, как перекопировать свежее из git-репо (см. entrypoint.sh) — файл ВНУТРИ этой папки
был бы тихо стёрт при первом же следующем деплое. Здесь, рядом с clients/, entrypoint его не
трогает вообще (тот же приём, что уже используется для session_snapshot.json).

Нет GitHub-токена, нет сетевых вызовов — просто локальный JSON на том же volume, что и так
уже переживает рестарт/редеплой контейнера (тот же диск, что бэкапит Timeweb)."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from .models import DaySchedule


logger = logging.getLogger(__name__)

_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WEEKDAY_LABELS_RU = {
    "mon": "Пн", "tue": "Вт", "wed": "Ср", "thu": "Чт", "fri": "Пт", "sat": "Сб", "sun": "Вс",
}
# Дублирует knowledge.CLIENT_ID_PATTERN (не импортируем оттуда — knowledge.py сам импортирует
# этот модуль, взаимный импорт получился бы циклическим). Сегодня единственный вызывающий
# (routes/settings.py) уже валидирует company_id через resolver.get(..., fallback=False) ДО
# того, как дойти до save_overrides_atomic — но это защита на будущее, если когда-нибудь
# появится второй вызывающий без такой проверки: без неё company_id вроде "../../etc/passwd"
# сконструировал бы путь, убегающий из overrides_dir.
_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _overrides_path(overrides_dir: Path, company_id: str) -> Path:
    if not _CLIENT_ID_PATTERN.fullmatch(company_id):
        raise ValueError(f"invalid company_id: {company_id!r}")
    return overrides_dir / f"{company_id}.json"


def _backup_path(overrides_dir: Path, company_id: str) -> Path:
    """Файл "предыдущей версии" — состояние оверрайда ПЕРЕД последним сохранением, для кнопки
    "↺ Отменить" по блокам в "Настройках" (см. reset_block). Один уровень отмены, не полная
    история — обсуждено с пользователем 2026-08-29."""

    if not _CLIENT_ID_PATTERN.fullmatch(company_id):
        raise ValueError(f"invalid company_id: {company_id!r}")
    return overrides_dir / f"{company_id}.previous.json"


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as error:
        logger.warning(
            "config_overrides: не смог прочитать %s, откат на пустые данные error=%s",
            path,
            type(error).__name__,
        )
        return {}


def load_overrides(overrides_dir: Path, company_id: str) -> dict[str, Any]:
    """Читает оверрайды клиента. Отсутствующий файл — норма (никто ещё ничего не менял через
    "Настройки"), возвращает {}. Битый/нечитаемый файл — тоже {}, с warning в лог, а НЕ
    исключение: кривой файл настроек не должен ронять загрузку всей базы знаний клиента,
    только отменять сами оверрайды (откат на данные из git, как будто их не было)."""

    try:
        path = _overrides_path(overrides_dir, company_id)
    except ValueError:
        # Невалидный company_id сюда сегодня не долетает (routes/settings.py валидирует его
        # раньше, через resolver.get(..., fallback=False)) — но эта функция обещает никогда
        # не бросать исключения, так что и этот случай тоже просто "оверрайдов нет".
        return {}
    return _load_json_dict(path)


def load_previous_backup(overrides_dir: Path, company_id: str) -> dict[str, Any]:
    """Аналог load_overrides, но для файла-бэкапа "предыдущей версии" (см. _backup_path и
    reset_block) — тот же контракт: никогда не бросает исключение, отсутствующий/битый файл —
    просто "предыдущей версии нет" (например, самое первое сохранение вообще)."""

    try:
        path = _backup_path(overrides_dir, company_id)
    except ValueError:
        return {}
    return _load_json_dict(path)


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def save_overrides_atomic(overrides_dir: Path, company_id: str, data: dict[str, Any]) -> None:
    """Атомарная запись: пишем во временный файл РЯДОМ, затем os.replace() поверх настоящего.
    os.replace — атомарная операция на POSIX (и на Windows начиная с той же гарантии) — если
    процесс упадёт посреди записи, останется либо старый файл целиком, либо новый целиком,
    никогда битая полу-запись.

    Перед перезаписью сохраняем ТО, что было на диске до этого сохранения, в отдельный
    файл-бэкап (_backup_path) — единственный уровень отмены для "↺ Отменить" в "Настройках"
    (см. reset_block). Через load_overrides (а не сырое чтение) — гарантированно валидный
    словарь, даже если то, что было на диске, уже было битым."""

    overrides_dir.mkdir(parents=True, exist_ok=True)
    path = _overrides_path(overrides_dir, company_id)
    _atomic_write_json(_backup_path(overrides_dir, company_id), load_overrides(overrides_dir, company_id))
    _atomic_write_json(path, data)


def format_working_hours_text(schedule: dict[str, Optional[DaySchedule]]) -> str:
    """Собирает читаемый текст расписания из структурированного schedule — группирует ПОДРЯД
    идущие дни с одинаковым временем (или одинаково закрытые) в диапазон, чтобы получить
    "Пн-Пт 9:00-20:00, Сб-Вс выходной", а не 7 строк по одной на день. Тот же формат, что уже
    руками писали в company.yaml (см. живой пример стухшего "Пн-Пт 9:00-20:00, Сб 10:00-18:00"
    из ROSH до синхронизации) — теперь генерируется, а не набирается вручную, чтобы эти два
    поля больше не могли разъехаться сами по себе."""

    def _day_key(day: str) -> tuple[str, str] | None:
        entry = schedule.get(day)
        if entry is None:
            return None
        return (entry.open, entry.close)

    groups: list[tuple[list[str], tuple[str, str] | None]] = []
    for day in _WEEKDAY_KEYS:
        key = _day_key(day)
        if groups and groups[-1][1] == key:
            groups[-1][0].append(day)
        else:
            groups.append(([day], key))

    parts = []
    for days, key in groups:
        label = _WEEKDAY_LABELS_RU[days[0]] if len(days) == 1 else (
            f"{_WEEKDAY_LABELS_RU[days[0]]}-{_WEEKDAY_LABELS_RU[days[-1]]}"
        )
        if key is None:
            parts.append(f"{label} выходной")
        else:
            parts.append(f"{label} {key[0]}-{key[1]}")
    return ", ".join(parts)


def apply_company_overrides(company: Any, override: dict[str, Any]) -> None:
    """Мутирует уже загруженный CompanyConfig оверрайдами (CompanyConfig не frozen — уже
    проверено и используется в тестах этой сессии). working_hours (текст для фраз) всегда
    пересчитывается ИЗ working_hours_schedule, если она пришла в оверрайде — не даём этим
    двум полям снова разъехаться, как уже случалось руками в company.yaml."""

    if "phone" in override:
        company.phone = str(override["phone"])
    if "address" in override:
        company.address = str(override["address"])
    if "telegram_url" in override:
        company.telegram_url = str(override["telegram_url"])
    if "website_url" in override:
        company.website_url = str(override["website_url"])
    if "working_hours_schedule" in override:
        schedule = {
            day: (DaySchedule(**value) if value is not None else None)
            for day, value in override["working_hours_schedule"].items()
        }
        company.working_hours_schedule = schedule
        company.working_hours = format_working_hours_text(schedule)


def apply_config_payload_overrides(config_payload: dict[str, Any], override: dict[str, Any]) -> None:
    """Мутирует уже загруженный config_payload (обычный dict из yaml.safe_load) — widget/facts
    точечно по ключам (не заменяем секцию целиком, чтобы не потерять поля, которых нет в форме
    настроек). doctors — ИСКЛЮЧЕНИЕ: список целиком, не точечный мёрж — как только админ хоть
    раз сохранил через форму, весь список из формы становится источником правды (то, что было
    в git — только стартовые данные для первого показа формы). Мёржить списки построчно (по
    какому ключу — по имени? по индексу?) не имеет однозначного смысла, в отличие от плоских
    полей."""

    if "widget" in override:
        widget = config_payload.setdefault("widget", {})
        if isinstance(widget, dict):
            widget.update(override["widget"])
    if "facts" in override:
        clinic_info = config_payload.setdefault("clinic_info", {})
        if isinstance(clinic_info, dict):
            facts = clinic_info.setdefault("facts", {})
            if isinstance(facts, dict):
                facts.update(override["facts"])
    if "doctors" in override:
        clinic_info = config_payload.setdefault("clinic_info", {})
        if isinstance(clinic_info, dict):
            clinic_info["doctors"] = override["doctors"]


_MISSING = object()


def _get_nested(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return _MISSING
        cur = cur[key]
    return cur


def _set_nested(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cur = data
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def _delete_nested(data: dict[str, Any], path: tuple[str, ...]) -> None:
    cur: Any = data
    for key in path[:-1]:
        if not isinstance(cur, dict) or key not in cur:
            return
        cur = cur[key]
    if isinstance(cur, dict):
        cur.pop(path[-1], None)


# Соответствие карточек "Настроек" путям внутри override-словаря. "company" в оверрайде —
# общий кусок сразу для ДВУХ карточек (часы и контакты), поэтому reset одной карточки не
# должен трогать поля другой — сравниваем и переносим конкретные поля, а не секцию целиком.
_RESET_BLOCKS: dict[str, tuple[tuple[str, ...], ...]] = {
    "hours": (("company", "working_hours_schedule"),),
    "contacts": (
        ("company", "phone"),
        ("company", "address"),
        ("company", "telegram_url"),
        ("company", "website_url"),
    ),
    "widget": (("widget",),),
    "facts": (("facts",),),
    "doctors": (("doctors",),),
}


def reset_block(overrides_dir: Path, company_id: str, block: str) -> dict[str, Any]:
    """Откат ОДНОГО блока "Настроек" к состоянию перед последним сохранением — использует
    бэкап, который save_overrides_atomic пишет на каждое сохранение (см. там). Один уровень
    отмены, не полная история: повторный reset того же блока просто качнёт значение обратно
    (сам reset тоже проходит через save_overrides_atomic и создаёт свежий бэкап) — это
    осознанно, не баг, обсуждено с пользователем 2026-08-29.

    Если поля не было и в бэкапе (например, это вообще самое первое сохранение) — поле
    удаляется из оверрайда целиком, откатываясь на данные из гита, а не оставляя пустоту."""

    if block not in _RESET_BLOCKS:
        raise ValueError(f"unknown settings block: {block!r}")

    current = load_overrides(overrides_dir, company_id)
    previous = load_previous_backup(overrides_dir, company_id)

    for path in _RESET_BLOCKS[block]:
        value = _get_nested(previous, path)
        if value is _MISSING:
            _delete_nested(current, path)
        else:
            _set_nested(current, path, value)

    save_overrides_atomic(overrides_dir, company_id, current)
    return current
