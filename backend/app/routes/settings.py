"""Веб-редактор базовых настроек клиента ("Настройки"-таб, TSK-06, 2026-08-29) — часы работы,
контакты, брендинг виджета, факты клиники. Сохранение — локальный JSON-оверрайд
(app/config_overrides.py), без GitHub-токена и без сети, см. модуль для того, почему именно
так и почему НЕ внутри clients/<id>."""

from __future__ import annotations

import re
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from .. import config_overrides
from ..auth import verify_operator_token
from ..editable_texts import (
    EDITABLE_TEXTS,
    FORBIDDEN_PATTERN,
    GROUPS,
    MAX_BUTTON_LABEL_LENGTH,
    MAX_VARIANTS,
    OPERATOR_WAIT_OFFER_MINUTES_RANGE,
    RENAMABLE_BUTTONS,
    button_renames,
    operator_wait_offer_minutes,
    validate_text,
)
from ..knowledge import DEFAULT_WIDGET_CONFIG, HEX_COLOR_PATTERN


router = APIRouter(prefix="/api/settings", tags=["settings"])

# Карточки "Настроек", для которых доступна кнопка "↺ Отменить" (см. config_overrides.reset_block).
_RESET_BLOCK_NAMES = ("hours", "contacts", "widget", "facts", "doctors", "texts", "buttons", "behavior")

_HH_MM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DEFAULT_FACTS = {
    "oms": False,
    "dms": False,
    "ambulance_brings": False,
    "sells_products": False,
    "discloses_doctor_schedule": False,
}


class DayScheduleInput(BaseModel):
    open: str
    close: str

    @field_validator("open", "close")
    @classmethod
    def _validate_hh_mm(cls, value: str) -> str:
        if not _HH_MM.fullmatch(value):
            raise ValueError(f"ожидался формат ЧЧ:ММ, получено {value!r}")
        return value


class WidgetInput(BaseModel):
    primary_color: str = Field(min_length=1)
    button_color: str = Field(min_length=1)
    header_title: str = Field(min_length=1)
    header_subtitle: str = ""
    position: str = "bottom-right"
    avatar_emoji: str = "💬"
    # None — поле не прислали (старая версия вкладки), тогда остаётся значение из данных клиента
    assistant_label: Optional[str] = Field(default=None, min_length=1, max_length=30)
    ai_badge: Optional[str] = None
    booking_highlight_color: Optional[str] = None
    launcher_label: Optional[str] = Field(default=None, min_length=1, max_length=30)
    status_online: Optional[str] = Field(default=None, min_length=1, max_length=30)
    input_placeholder: Optional[str] = Field(default=None, min_length=1, max_length=60)
    operator_label: Optional[str] = Field(default=None, min_length=1, max_length=30)

    @field_validator("ai_badge")
    @classmethod
    def _validate_ai_badge(cls, value: Optional[str]) -> Optional[str]:
        if value not in (None, "", "show"):
            raise ValueError(f"ai_badge: ожидалось пусто или show, получено {value!r}")
        return value

    @field_validator("booking_highlight_color")
    @classmethod
    def _validate_highlight_color(cls, value: Optional[str]) -> Optional[str]:
        if value and not HEX_COLOR_PATTERN.fullmatch(value.strip()):
            raise ValueError(f"цвет рамки: ожидался формат #RRGGBB, получено {value!r}")
        return value.strip() if value else value

    @field_validator("position")
    @classmethod
    def _validate_position(cls, value: str) -> str:
        allowed = {"bottom-right", "bottom-left"}
        if value not in allowed:
            raise ValueError(f"position должен быть одним из {sorted(allowed)}, получено {value!r}")
        return value


class FactsInput(BaseModel):
    oms: bool = False
    dms: bool = False
    ambulance_brings: bool = False
    sells_products: bool = False
    discloses_doctor_schedule: bool = False


class DoctorInput(BaseModel):
    name: str = Field(min_length=1)
    specialty: str = ""
    schedule: str = ""


class CompanySettingsInput(BaseModel):
    phone: str = Field(min_length=1)
    address: Optional[str] = None
    telegram_url: Optional[str] = None
    website_url: Optional[str] = None
    working_hours_schedule: dict[str, Optional[DayScheduleInput]]
    widget: WidgetInput
    facts: FactsInput
    # Список целиком, не точечный мёрж — см. apply_config_payload_overrides.
    doctors: list[DoctorInput] = Field(default_factory=list)
    # новые блоки (2026-09-23); None — старая версия вкладки их не прислала, сохранённое не трогаем
    privacy_policy_url: Optional[str] = None
    texts: Optional[dict[str, list[str]]] = None
    button_labels: Optional[dict[str, str]] = None
    operator_wait_offer_minutes: Optional[int] = None

    @field_validator("privacy_policy_url")
    @classmethod
    def _validate_policy_url(cls, value: Optional[str]) -> Optional[str]:
        value = (value or "").strip()
        if value and not value.startswith("https://"):
            raise ValueError("ссылка на политику должна начинаться с https://")
        return value

    @field_validator("button_labels")
    @classmethod
    def _validate_button_labels(cls, value: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
        if value is None:
            return None
        cleaned: dict[str, str] = {}
        for original, label in value.items():
            if original not in RENAMABLE_BUTTONS:
                raise ValueError(f"эту кнопку переименовать нельзя: {original!r}")
            label = label.strip()
            if not label or label == original:
                continue
            if len(label) > MAX_BUTTON_LABEL_LENGTH or FORBIDDEN_PATTERN.search(label) or "{" in label:
                raise ValueError(f"кнопка «{original}»: до {MAX_BUTTON_LABEL_LENGTH} символов, без ссылок и скобок")
            cleaned[original] = label
        return cleaned

    @field_validator("operator_wait_offer_minutes")
    @classmethod
    def _validate_wait_minutes(cls, value: Optional[int]) -> Optional[int]:
        low, high = OPERATOR_WAIT_OFFER_MINUTES_RANGE
        if value is not None and not low <= value <= high:
            raise ValueError(f"минуты ожидания: от {low} до {high}")
        return value

    @field_validator("working_hours_schedule")
    @classmethod
    def _validate_weekday_keys(cls, value: dict) -> dict:
        unknown = set(value) - set(_WEEKDAY_KEYS)
        if unknown:
            raise ValueError(f"неизвестные дни недели: {sorted(unknown)}")
        return value


def _as_variants(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if str(value or "").strip() else []


def _texts_overrides(texts: dict[str, list[str]], base: dict[str, object]) -> dict[str, object]:
    """проверяет тексты и оставляет только те, что отличаются от текстов клиента/по умолчанию —
    иначе поправка в данных клиента потом молча перекрывалась бы старой копией из «Настроек»."""

    overrides: dict[str, object] = {}
    errors: list[str] = []
    for key, variants in texts.items():
        if key not in EDITABLE_TEXTS:
            errors.append(f"текст {key!r} менять нельзя")
            continue
        cleaned = [variant.strip() for variant in variants if variant.strip()][:MAX_VARIANTS]
        if not cleaned or cleaned == _as_variants(base.get(key)):
            continue
        for variant in cleaned:
            problem = validate_text(key, variant)
            if problem:
                errors.append(f"«{EDITABLE_TEXTS[key].label}»: {problem}")
                break
        overrides[key] = cleaned[0] if len(cleaned) == 1 else cleaned
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))
    return overrides


def _texts_view(base: dict[str, object], effective: dict[str, object]) -> list[dict[str, object]]:
    return [
        {
            "title": title,
            "items": [
                {
                    "key": item.key,
                    "label": item.label,
                    "where": item.where,
                    "placeholders": list(item.placeholders),
                    "default": _as_variants(base.get(item.key)),
                    "value": _as_variants(effective.get(item.key)),
                    "customized": _as_variants(base.get(item.key)) != _as_variants(effective.get(item.key)),
                }
                for item in items
            ],
        }
        for title, items in GROUPS
    ]


def _current_settings(knowledge_base, resolver=None) -> dict[str, object]:
    company = knowledge_base.company
    widget = dict(DEFAULT_WIDGET_CONFIG)
    raw_widget = knowledge_base.config_payload.get("widget")
    if isinstance(raw_widget, dict):
        widget.update({key: raw_widget[key] for key in widget if key in raw_widget})

    facts = dict(_DEFAULT_FACTS)
    clinic_info = knowledge_base.config_payload.get("clinic_info")
    raw_facts = clinic_info.get("facts") if isinstance(clinic_info, dict) else None
    if isinstance(raw_facts, dict):
        facts.update({key: bool(raw_facts[key]) for key in facts if key in raw_facts})

    raw_doctors = clinic_info.get("doctors") if isinstance(clinic_info, dict) else None
    doctors = []
    if isinstance(raw_doctors, list):
        for item in raw_doctors:
            if not isinstance(item, dict):
                continue
            doctors.append(
                {
                    "name": str(item.get("name") or ""),
                    "specialty": str(item.get("specialty") or ""),
                    "schedule": str(item.get("schedule") or ""),
                }
            )

    renames = button_renames(knowledge_base.config_payload)
    base_phrasebook = resolver.base_phrasebook(company.company_id) if resolver is not None else knowledge_base.phrasebook
    return {
        "phone": company.phone,
        "address": company.address,
        "telegram_url": company.telegram_url,
        "website_url": company.website_url,
        "privacy_policy_url": company.privacy_policy_url or "",
        "texts": _texts_view(base_phrasebook, knowledge_base.phrasebook),
        "button_labels": [{"original": original, "label": renames.get(original, "")} for original in RENAMABLE_BUTTONS],
        "operator_wait_offer_minutes": operator_wait_offer_minutes(knowledge_base.config_payload),
        "working_hours_schedule": {
            day: ({"open": entry.open, "close": entry.close} if entry is not None else None)
            for day, entry in company.working_hours_schedule.items()
        }
        if company.working_hours_schedule
        else {day: None for day in _WEEKDAY_KEYS},
        "widget": widget,
        "facts": facts,
        "doctors": doctors,
    }


@router.get("/company")
async def get_company_settings(
    request: Request,
    company_id: str = Query(...),
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, object]:
    verify_operator_token(request, x_operator_token)
    resolver = request.app.state.knowledge_base_resolver
    try:
        knowledge_base = resolver.get(company_id, fallback=False)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown company") from error
    return _current_settings(knowledge_base, resolver)


@router.post("/company")
async def save_company_settings(
    payload: CompanySettingsInput,
    request: Request,
    company_id: str = Query(...),
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, object]:
    verify_operator_token(request, x_operator_token)
    resolver = request.app.state.knowledge_base_resolver
    try:
        resolver.get(company_id, fallback=False)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown company") from error

    settings = request.app.state.settings
    previous = config_overrides.load_overrides(settings.overrides_dir, company_id)
    override = {
        "company": {
            "phone": payload.phone,
            "address": payload.address,
            "telegram_url": payload.telegram_url,
            "website_url": payload.website_url,
            "working_hours_schedule": {
                day: (schedule.model_dump() if schedule is not None else None)
                for day, schedule in payload.working_hours_schedule.items()
            },
        },
        "widget": payload.widget.model_dump(exclude_none=True),
        "facts": payload.facts.model_dump(),
        "doctors": [doctor.model_dump() for doctor in payload.doctors],
    }
    # новые блоки: прислали — берём, не прислали (старая версия вкладки) — оставляем сохранённое
    if payload.privacy_policy_url is not None:
        override["company"]["privacy_policy_url"] = payload.privacy_policy_url
    elif "privacy_policy_url" in previous.get("company", {}):
        override["company"]["privacy_policy_url"] = previous["company"]["privacy_policy_url"]
    if payload.texts is not None:
        override["texts"] = _texts_overrides(payload.texts, resolver.base_phrasebook(company_id))
    elif "texts" in previous:
        override["texts"] = previous["texts"]
    if payload.button_labels is not None:
        override["button_labels"] = payload.button_labels
    elif "button_labels" in previous:
        override["button_labels"] = previous["button_labels"]
    if payload.operator_wait_offer_minutes is not None:
        override["operator"] = {"wait_offer_minutes": payload.operator_wait_offer_minutes}
    elif "operator" in previous:
        override["operator"] = previous["operator"]
    config_overrides.save_overrides_atomic(settings.overrides_dir, company_id, override)
    resolver.invalidate(company_id)

    return _current_settings(resolver.get(company_id, fallback=False), resolver)


@router.post("/company/reset-block")
async def reset_company_settings_block(
    request: Request,
    company_id: str = Query(...),
    block: str = Query(...),
    x_operator_token: Optional[str] = Header(default=None),
) -> dict[str, object]:
    """Кнопка "↺ Отменить" у отдельной карточки — один шаг назад для ЭТОГО блока (см.
    config_overrides.reset_block: использует бэкап "предыдущей версии", который
    save_overrides_atomic пишет на каждое сохранение). Другие блоки не трогает, даже если их
    сохраняли позже."""

    verify_operator_token(request, x_operator_token)
    if block not in _RESET_BLOCK_NAMES:
        raise HTTPException(status_code=422, detail=f"unknown block: {block!r}")

    resolver = request.app.state.knowledge_base_resolver
    try:
        resolver.get(company_id, fallback=False)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Unknown company") from error

    settings = request.app.state.settings
    config_overrides.reset_block(settings.overrides_dir, company_id, block)
    resolver.invalidate(company_id)

    return _current_settings(resolver.get(company_id, fallback=False), resolver)
