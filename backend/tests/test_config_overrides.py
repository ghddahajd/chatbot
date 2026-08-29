"""config_overrides.py — локальные оверрайды "Настройки"-таба (TSK-06, 2026-08-29).

Ключевой сценарий, который тут проверяется: файл переживает "редеплой" — docker-entrypoint.sh
делает rm -rf ИМЕННО на backend/data/clients/<id>, не трогая ничего рядом. Оверрайды лежат вне
этой папки специально ради этого — тест test_survives_client_dir_wipe симулирует ровно это."""

import json

import pytest

from app.config_overrides import (
    apply_company_overrides,
    apply_config_payload_overrides,
    format_working_hours_text,
    load_overrides,
    save_overrides_atomic,
)
from app.models import CompanyConfig, DaySchedule


def _company(**overrides) -> CompanyConfig:
    base = {
        "company_id": "test",
        "company_name": "Test",
        "city": "Москва",
        "working_hours": "старый текст",
        "phone": "+7 000",
    }
    base.update(overrides)
    return CompanyConfig(**base)


def test_load_overrides_returns_empty_dict_when_file_missing(tmp_path) -> None:
    assert load_overrides(tmp_path, "rosh_import_demo") == {}


def test_save_then_load_round_trips(tmp_path) -> None:
    save_overrides_atomic(tmp_path, "rosh_import_demo", {"company": {"phone": "+7 999"}})
    assert load_overrides(tmp_path, "rosh_import_demo") == {"company": {"phone": "+7 999"}}


def test_load_overrides_survives_corrupt_file_without_raising(tmp_path) -> None:
    """Живой сценарий, который пользователь явно попросил проверить: сервер упал посреди
    записи (в теории — до атомарной записи это было бы реальностью). Битый JSON не должен
    ронять загрузку клиента, только откатывать на данные из git."""

    path = tmp_path / "rosh_import_demo.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert load_overrides(tmp_path, "rosh_import_demo") == {}


def test_load_overrides_survives_empty_file(tmp_path) -> None:
    path = tmp_path / "rosh_import_demo.json"
    path.write_text("", encoding="utf-8")
    assert load_overrides(tmp_path, "rosh_import_demo") == {}


def test_load_overrides_survives_non_dict_json(tmp_path) -> None:
    path = tmp_path / "rosh_import_demo.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert load_overrides(tmp_path, "rosh_import_demo") == {}


def test_save_overrides_atomic_leaves_no_tmp_file_behind(tmp_path) -> None:
    save_overrides_atomic(tmp_path, "rosh_import_demo", {"company": {"phone": "+7 999"}})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["rosh_import_demo.json"]


def test_save_overrides_survives_client_dir_wipe(tmp_path) -> None:
    """Симулирует ровно то, что делает docker-entrypoint.sh при деплое: rm -rf на всю папку
    клиента. Оверрайды лежат СНАРУЖИ этой папки — этот rm -rf их не должен трогать."""

    overrides_dir = tmp_path / "overrides"
    client_dir = tmp_path / "clients" / "rosh_import_demo"
    client_dir.mkdir(parents=True)
    (client_dir / "company.yaml").write_text("company_id: rosh_import_demo\n", encoding="utf-8")

    save_overrides_atomic(overrides_dir, "rosh_import_demo", {"company": {"phone": "+7 999"}})

    import shutil

    shutil.rmtree(client_dir)
    client_dir.mkdir(parents=True)
    (client_dir / "company.yaml").write_text("company_id: rosh_import_demo\nphone: old\n", encoding="utf-8")

    assert load_overrides(overrides_dir, "rosh_import_demo") == {"company": {"phone": "+7 999"}}


def test_format_working_hours_text_groups_identical_consecutive_days() -> None:
    schedule = {day: DaySchedule(open="10:00", close="21:00") for day in
                ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]}
    assert format_working_hours_text(schedule) == "Пн-Вс 10:00-21:00"


def test_format_working_hours_text_splits_different_weekday_and_weekend_hours() -> None:
    weekday = DaySchedule(open="09:00", close="20:00")
    weekend = DaySchedule(open="10:00", close="18:00")
    schedule = {
        "mon": weekday, "tue": weekday, "wed": weekday, "thu": weekday, "fri": weekday,
        "sat": weekend, "sun": weekend,
    }
    assert format_working_hours_text(schedule) == "Пн-Пт 09:00-20:00, Сб-Вс 10:00-18:00"


def test_format_working_hours_text_handles_closed_day() -> None:
    open_day = DaySchedule(open="10:00", close="20:00")
    schedule = {
        "mon": open_day, "tue": open_day, "wed": open_day, "thu": open_day, "fri": open_day,
        "sat": open_day, "sun": None,
    }
    assert format_working_hours_text(schedule) == "Пн-Сб 10:00-20:00, Вс выходной"


def test_format_working_hours_text_handles_single_odd_day_out() -> None:
    weekday = DaySchedule(open="10:00", close="21:00")
    friday = DaySchedule(open="10:00", close="19:00")
    schedule = {
        "mon": weekday, "tue": weekday, "wed": weekday, "thu": weekday, "fri": friday,
        "sat": weekday, "sun": weekday,
    }
    assert format_working_hours_text(schedule) == "Пн-Чт 10:00-21:00, Пт 10:00-19:00, Сб-Вс 10:00-21:00"


def test_apply_company_overrides_mutates_in_place_and_regenerates_text() -> None:
    company = _company()
    override = {
        "phone": "+7 999",
        "address": "новый адрес",
        "working_hours_schedule": {
            day: {"open": "08:00", "close": "22:00"}
            for day in ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        },
    }
    apply_company_overrides(company, override)

    assert company.phone == "+7 999"
    assert company.address == "новый адрес"
    assert company.working_hours == "Пн-Вс 08:00-22:00"
    assert company.working_hours_schedule["mon"].open == "08:00"


def test_apply_company_overrides_leaves_untouched_fields_alone() -> None:
    company = _company(address="старый адрес")
    apply_company_overrides(company, {"phone": "+7 999"})
    assert company.address == "старый адрес"


def test_apply_config_payload_overrides_merges_widget_without_dropping_other_keys() -> None:
    config_payload = {"widget": {"primary_color": "#000000", "extra_untouched_key": "keep me"}}
    apply_config_payload_overrides(config_payload, {"widget": {"primary_color": "#ffffff"}})
    assert config_payload["widget"]["primary_color"] == "#ffffff"
    assert config_payload["widget"]["extra_untouched_key"] == "keep me"


def test_apply_config_payload_overrides_creates_clinic_info_facts_if_absent() -> None:
    config_payload: dict = {}
    apply_config_payload_overrides(config_payload, {"facts": {"oms": True}})
    assert config_payload["clinic_info"]["facts"]["oms"] is True


def test_load_overrides_rejects_path_traversal_company_id_gracefully(tmp_path) -> None:
    """Защита на будущее (сегодня routes/settings.py валидирует company_id раньше через
    resolver.get(..., fallback=False), сюда такое не долетает) — но load_overrides обещает
    никогда не бросать исключения, так что и path-traversal company_id тоже просто "нет
    оверрайдов", а не краш."""

    assert load_overrides(tmp_path, "../../etc/passwd") == {}


def test_save_overrides_raises_on_path_traversal_company_id(tmp_path) -> None:
    """В отличие от чтения — запись с невалидным company_id должна упасть громко (это
    write-путь, тихо промолчать и НЕ сохранить было бы хуже, чем честная ошибка)."""

    with pytest.raises(ValueError):
        save_overrides_atomic(tmp_path, "../../etc/passwd", {"company": {}})
