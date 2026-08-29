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
from ..knowledge import DEFAULT_WIDGET_CONFIG


router = APIRouter(prefix="/api/settings", tags=["settings"])

# Карточки "Настроек", для которых доступна кнопка "↺ Отменить" (см. config_overrides.reset_block).
_RESET_BLOCK_NAMES = ("hours", "contacts", "widget", "facts", "doctors")

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

    @field_validator("working_hours_schedule")
    @classmethod
    def _validate_weekday_keys(cls, value: dict) -> dict:
        unknown = set(value) - set(_WEEKDAY_KEYS)
        if unknown:
            raise ValueError(f"неизвестные дни недели: {sorted(unknown)}")
        return value


def _current_settings(knowledge_base) -> dict[str, object]:
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

    return {
        "phone": company.phone,
        "address": company.address,
        "telegram_url": company.telegram_url,
        "website_url": company.website_url,
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
    return _current_settings(knowledge_base)


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
        "widget": payload.widget.model_dump(),
        "facts": payload.facts.model_dump(),
        "doctors": [doctor.model_dump() for doctor in payload.doctors],
    }
    settings = request.app.state.settings
    config_overrides.save_overrides_atomic(settings.overrides_dir, company_id, override)
    resolver.invalidate(company_id)

    return _current_settings(resolver.get(company_id, fallback=False))


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

    return _current_settings(resolver.get(company_id, fallback=False))
