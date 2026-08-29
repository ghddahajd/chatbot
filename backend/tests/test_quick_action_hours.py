"""format_quick_actions — "Позвать менеджера" вне рабочих часов (2026-08-29, продолжение TSK-02).

Живой баг из беклога: кнопка "Позвать менеджера" захардкожена ~48 раз по policy/__init__.py,
ни одно из этих мест не знает про часы работы. Ночью клик по ней всё равно корректно доезжает
до after-hours хука в policy (просит контакт, не обещает живого оператора) — но САМА КНОПКА до
клика продолжает звать "позвать менеджера", как будто он сейчас ответит. Фикс — не трогать 48
мест, а подменять ЛЕЙБЛ в единственной точке форматирования (format_quick_actions), которая уже
видит company.working_hours_schedule/timezone."""

from app.models import DaySchedule
from app.routes.chat_utils import format_quick_actions

_ALWAYS_CLOSED_SCHEDULE = {
    "mon": None,
    "tue": None,
    "wed": None,
    "thu": None,
    "fri": None,
    "sat": None,
    "sun": None,
}


def _close_all_day(knowledge_base) -> None:
    knowledge_base.company.working_hours_schedule = _ALWAYS_CLOSED_SCHEDULE
    knowledge_base.company.timezone = "Europe/Moscow"


def test_operator_button_unchanged_when_schedule_is_empty_ie_always_open(knowledge_base) -> None:
    # Дефолт для большинства клиентов (пустое расписание) — always open, поведение не меняется.
    actions = format_quick_actions(["Позвать менеджера", "Посмотреть услуги"], None, knowledge_base)
    assert [a.label for a in actions] == ["Позвать менеджера", "Посмотреть услуги"]
    assert actions[0].value == "Хочу поговорить с менеджером"


def test_operator_button_unchanged_during_business_hours(knowledge_base) -> None:
    knowledge_base.company.working_hours_schedule = {
        "mon": DaySchedule(open="00:00", close="23:59"),
        "tue": DaySchedule(open="00:00", close="23:59"),
        "wed": DaySchedule(open="00:00", close="23:59"),
        "thu": DaySchedule(open="00:00", close="23:59"),
        "fri": DaySchedule(open="00:00", close="23:59"),
        "sat": DaySchedule(open="00:00", close="23:59"),
        "sun": DaySchedule(open="00:00", close="23:59"),
    }
    knowledge_base.company.timezone = "Europe/Moscow"
    actions = format_quick_actions(["Позвать менеджера"], None, knowledge_base)
    assert actions[0].label == "Позвать менеджера"


def test_operator_button_relabeled_to_leave_phone_when_closed(knowledge_base) -> None:
    _close_all_day(knowledge_base)
    actions = format_quick_actions(["Позвать менеджера", "Посмотреть услуги"], None, knowledge_base)
    assert [a.label for a in actions] == ["Оставить телефон", "Посмотреть услуги"]
    # value не трогаем — сообщение всё так же уходит как "Хочу поговорить с менеджером", чтобы
    # сработал честный after-hours ASK_CONTACT в policy, а не generic contact_prompt.
    assert actions[0].value == "Хочу поговорить с менеджером"
    assert actions[0].type == "message"


def test_relabeled_button_deduped_against_a_real_leave_phone_button_when_closed(knowledge_base) -> None:
    _close_all_day(knowledge_base)
    actions = format_quick_actions(
        ["Оставить телефон", "Позвать менеджера"], None, knowledge_base
    )
    labels = [a.label for a in actions]
    assert labels == ["Оставить телефон"]
    # Победила первая по порядку — настоящая "Оставить телефон" со своим обычным value.
    assert actions[0].value == "Хочу оставить телефон"


def test_relabeled_button_deduped_regardless_of_order_when_closed(knowledge_base) -> None:
    _close_all_day(knowledge_base)
    actions = format_quick_actions(
        ["Позвать менеджера", "Оставить телефон"], None, knowledge_base
    )
    assert [a.label for a in actions] == ["Оставить телефон"]
    # Тут первой оказалась бывшая "Позвать менеджера" — её value и остаётся у выжившей кнопки.
    assert actions[0].value == "Хочу поговорить с менеджером"


def test_generic_dedup_also_covers_unrelated_duplicate_dict_labels(knowledge_base) -> None:
    # Не спецкейс под конкретную пару кнопок — общая защита от дублей по итоговому лейблу.
    actions = format_quick_actions(
        [
            {"label": "Написать нам", "type": "message", "value": "первое"},
            {"label": "написать нам", "type": "message", "value": "второе"},
        ],
        None,
        knowledge_base,
    )
    assert len(actions) == 1
    assert actions[0].value == "первое"
