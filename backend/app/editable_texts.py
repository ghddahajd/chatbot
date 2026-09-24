"""тексты бота, которые клиника меняет сама во вкладке «Настройки».

Кризисные, медицинские, ценовые тексты и ответы по фактам (ОМС, ДМС…) сюда не входят —
их правим только мы, с прогоном сети безопасности. Подстановки ({phone} и т. п.) разрешены
только те, что код реально передаёт для этого текста, — иначе человек увидел бы сырое «{phone}».
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_TEXT_LENGTH = 600
MAX_VARIANTS = 3
PLACEHOLDER_PATTERN = re.compile(r"\{([a-z_]+)\}")
# ссылки и разметку не принимаем: виджет показывает текст как есть
FORBIDDEN_PATTERN = re.compile(r"<[a-zA-Z/!]|https?://|www\.", re.IGNORECASE)


@dataclass(frozen=True)
class EditableText:
    key: str
    label: str
    where: str
    placeholders: tuple[str, ...] = ()


GROUPS: tuple[tuple[str, tuple[EditableText, ...]], ...] = (
    (
        "Приветствие и о клинике",
        (
            EditableText("greeting", "Приветствие", "Первое сообщение, когда человек открывает чат, — над стартовыми карточками."),
            EditableText(
                "company_overview",
                "О клинике",
                "Ответ на общий вопрос: «расскажите о клинике», «чем вы занимаетесь».",
                ("company_name", "company_type", "city", "working_hours"),
            ),
            EditableText(
                "clinic_contacts",
                "Телефон и контакты",
                "Ответ на «какой у вас телефон», «как с вами связаться».",
                ("company_name", "phone", "address", "working_hours"),
            ),
            EditableText(
                "clinic_location",
                "Адрес и часы работы",
                "Ответ на «где вы находитесь», «как добраться», «до скольки работаете».",
                ("company_name", "city", "address", "working_hours"),
            ),
        ),
    ),
    (
        "Запись",
        (
            EditableText("booking_when_prompt", "Вопрос о времени", "Первый шаг записи — над плитками «Сегодня / Завтра / На этой неделе / Другое»."),
            EditableText("booking_when_today", "Выбрали «Сегодня»", "После нажатия «Сегодня» в рабочее время."),
            EditableText("booking_when_today_closed", "«Сегодня», но клиника закрыта", "После нажатия «Сегодня» до открытия или после закрытия."),
            EditableText("booking_when_tomorrow", "Выбрали «Завтра»", "После нажатия «Завтра»."),
            EditableText("booking_when_later", "Выбрали «На этой неделе» или «Другое»", "После нажатия «На этой неделе» или «Другое»."),
            EditableText(
                "booking_phone_prompt",
                "Просьба телефона",
                "Когда время уже названо («запишите на вторник») или вместо номера написали что-то другое.",
            ),
            EditableText("booking_success", "Заявка на процедуру принята", "После телефона, если записывались на процедуру."),
            EditableText("booking_success_no_consultation", "Заявка на консультацию принята", "После телефона, если записывались на консультацию."),
            EditableText("booking_cancelled", "Отказ от записи", "Если на шаге записи человек пишет «нет», «не надо»."),
            EditableText(
                "booking_bridge",
                "Добавка про запись",
                "Дописывается к ответу, если в одном сообщении спросили и про услугу или цену, и про запись.",
            ),
            EditableText("booking_hesitation_hold", "«Подумаю» на шаге записи", "Если на шаге записи человек сомневается."),
            EditableText(
                "booking_change_prompt",
                "Отменить или перенести запись",
                "Ответ на «отмените запись», «хочу перенести приём». Телефон клиники бот допишет сам.",
            ),
            EditableText("booking_change_success", "Просьба об отмене или переносе принята", "После того как человек оставил номер."),
            EditableText(
                "booking_change_known_phone",
                "Отмена или перенос, номер уже есть",
                "Если человек уже оставлял номер в этом чате — просьба уходит администратору сразу, без вопроса.",
            ),
        ),
    ),
    (
        "Контакт и заявка",
        (
            EditableText("contact_prompt", "Просьба оставить контакт", "Когда бот предлагает оставить имя и телефон не для записи — например, после ответа о цене."),
            EditableText("lead_success", "Контакт принят", "После того как человек оставил телефон не для записи."),
            EditableText("lead_followup", "Заявка уже есть", "Если человек снова спрашивает про запись, когда телефон уже оставил."),
            EditableText("contact_cancelled", "Отказ оставить контакт", "Если на просьбу контакта человек отвечает «нет»."),
            EditableText("frustration_recovery", "Извинение", "Если человек недоволен ответами бота («вы ничего не понимаете»)."),
        ),
    ),
    (
        "Связь с администратором",
        (
            EditableText("operator_soft_offer", "Первый ответ на «позовите оператора»", "Бот предлагает помочь сам или сразу соединить."),
            EditableText("operator_offer_declined_continue", "Остались с ботом", "Если после этого предложения человек решает продолжить в чате."),
            EditableText("handoff_message", "Передача администратору", "Когда диалог передаётся администратору в рабочее время."),
            EditableText(
                "operator_after_hours",
                "Администратор недоступен (нерабочее время)",
                "Просьба позвать администратора в нерабочее время.",
                ("working_hours",),
            ),
            EditableText(
                "operator_unreachable",
                "Нет связи с Telegram",
                "Если карточку администратору не удалось отправить из-за сбоя связи. Телефон клиники бот допишет сам.",
            ),
            EditableText("waiting_operator_ack", "Сообщение в ожидании", "Если человек пишет, пока ждёт администратора."),
            EditableText(
                "operator_wait_timeout_offer",
                "Администратор не подключился",
                "Если администратор не взял диалог за заданное время и человек пишет снова.",
            ),
            EditableText("operator_return_confirmed", "Вернулись к боту", "Если после этого предложения человек выбрал продолжить с ботом."),
            EditableText("complaint_escalation", "Жалоба", "Когда человек жалуется на сервис или врача — диалог сразу передаётся администратору."),
            EditableText("waiting_operator_complaint_ack", "Жалоба в ожидании", "Если человек снова жалуется, пока ждёт администратора."),
            EditableText("human_active_wait", "Администратор уже ведёт диалог", "Если человек пишет боту, когда диалог уже у администратора."),
            EditableText("engagement_offer_1", "Длинный диалог — предложить администратора (1-й раз)", "После 5 сообщений человека."),
            EditableText("engagement_offer_2", "Длинный диалог — предложить администратора (2-й раз)", "После 8 сообщений человека."),
            EditableText("engagement_offer_3", "Длинный диалог — предложить администратора (3-й раз)", "После 13 сообщений человека."),
            EditableText("engagement_continue", "Продолжили после предложения", "Если в длинном диалоге человек отказался от администратора."),
        ),
    ),
    (
        "Бот не понял или вопрос не по теме",
        (
            EditableText("clarify", "Уточнение", "Когда бот не понял, что человек хочет."),
            EditableText("off_topic", "Не по теме", "Вопрос не про клинику (погода, политика и т. п.)."),
            EditableText("unknown_service", "Нет такой услуги", "Спросили услугу, которой нет в прайсе."),
            EditableText(
                "unknown_service_named",
                "Нет такой услуги (с названием)",
                "То же, но бот называет услугу из вопроса.",
                ("service",),
            ),
            EditableText("doctors_deferred", "Про врачей", "Вопрос о врачах, когда в данных нет списка врачей."),
        ),
    ),
    (
        "Возражения",
        (
            EditableText("objection_price", "«Дорого»", "Когда человек говорит, что дорого."),
            EditableText("objection_hesitation", "«Подумаю»", "Когда человек сомневается или хочет подумать."),
            EditableText("objection_competitor", "«У других дешевле»", "Когда человек сравнивает с другими клиниками."),
            EditableText("objection_backoff", "«Не надо, потом»", "Если человек второй раз отказывается после возражения."),
        ),
    ),
)

EDITABLE_TEXTS: dict[str, EditableText] = {item.key: item for _, items in GROUPS for item in items}

# кнопки в ответах бота, которые клиника может переименовать; меняется только подпись —
# нажатие отправляет то же сообщение, бот понимает его как раньше
RENAMABLE_BUTTONS = ("Позвать менеджера", "Посмотреть услуги", "Оставить телефон", "Уточнить цену", "Написать в Telegram", "Открыть сайт")
MAX_BUTTON_LABEL_LENGTH = 30
DEFAULT_OPERATOR_WAIT_OFFER_MINUTES = 5
OPERATOR_WAIT_OFFER_MINUTES_RANGE = (1, 60)


def button_renames(config_payload: dict) -> dict[str, str]:
    raw = config_payload.get("button_labels") if isinstance(config_payload, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {
        original: str(label).strip()
        for original, label in raw.items()
        if original in RENAMABLE_BUTTONS and isinstance(label, str) and str(label).strip()
    }


def operator_wait_offer_minutes(config_payload: dict) -> int:
    operator = config_payload.get("operator") if isinstance(config_payload, dict) else None
    value = operator.get("wait_offer_minutes") if isinstance(operator, dict) else None
    low, high = OPERATOR_WAIT_OFFER_MINUTES_RANGE
    return value if isinstance(value, int) and low <= value <= high else DEFAULT_OPERATOR_WAIT_OFFER_MINUTES


def validate_text(key: str, value: str) -> str | None:
    """None — всё хорошо, иначе понятное объяснение для клиники."""

    item = EDITABLE_TEXTS[key]
    text = value.strip()
    if not text:
        return "текст не может быть пустым"
    if len(text) > MAX_TEXT_LENGTH:
        return f"не длиннее {MAX_TEXT_LENGTH} символов"
    if FORBIDDEN_PATTERN.search(text):
        return "без ссылок и HTML — виджет показывает текст как есть"
    unknown = sorted(set(PLACEHOLDER_PATTERN.findall(text)) - set(item.placeholders))
    if unknown:
        allowed = ", ".join("{" + name + "}" for name in item.placeholders) or "подстановки здесь не поддерживаются"
        return f"неизвестная подстановка {{{unknown[0]}}}; можно: {allowed}"
    if "{" in PLACEHOLDER_PATTERN.sub("", text) or "}" in PLACEHOLDER_PATTERN.sub("", text):
        return "фигурные скобки — только для подстановок вида {phone}"
    return None
