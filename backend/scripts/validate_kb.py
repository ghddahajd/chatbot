"""проверяет клиентскую базу знаний без запуска backend и зависимостей."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_FILES = ("company.yaml", "services.json", "prices.json", "faq.md")
REQUIRED_COMPANY_FIELDS = (
    "company_id",
    "company_name",
    "city",
    "working_hours",
    "phone",
    "medical_disclaimer",
)
REQUIRED_SERVICE_FIELDS = (
    "id",
    "name",
    "category",
    "synonyms",
    "short_description",
    "requires_specialist",
)


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_simple_yaml(path: Path) -> dict[str, object]:
    payload: dict[str, object] = {}
    current_list_key: str | None = None

    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("- "):
            if current_list_key is None:
                raise ValueError(f"{path.name}:{line_number}: list item without key")
            payload.setdefault(current_list_key, [])
            list_value = payload[current_list_key]
            if not isinstance(list_value, list):
                raise ValueError(f"{path.name}:{line_number}: key is not a list")
            list_value.append(_strip_quotes(stripped[2:]))
            continue

        if ":" not in stripped:
            raise ValueError(f"{path.name}:{line_number}: expected key: value")

        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ValueError(f"{path.name}:{line_number}: empty key")

        if not value:
            payload[key] = []
            current_list_key = key
            continue

        payload[key] = _strip_quotes(value)
        current_list_key = None

    return payload


def load_json_list(path: Path, label: str) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"{label}: expected top-level JSON array")
    if not all(isinstance(item, dict) for item in payload):
        raise ValueError(f"{label}: every item must be an object")
    return payload


def validate_kb(kb_dir: Path) -> list[str]:
    errors: list[str] = []

    if not kb_dir.exists():
        return [f"директория не найдена: {kb_dir}"]
    if not kb_dir.is_dir():
        return [f"это не директория: {kb_dir}"]

    for file_name in REQUIRED_FILES:
        file_path = kb_dir / file_name
        if not file_path.exists():
            errors.append(f"нет обязательного файла: {file_name}")
        elif not file_path.is_file():
            errors.append(f"ожидался файл, а не директория: {file_name}")

    if errors:
        return errors

    try:
        company = load_simple_yaml(kb_dir / "company.yaml")
        services = load_json_list(kb_dir / "services.json", "services.json")
        prices = load_json_list(kb_dir / "prices.json", "prices.json")
    except Exception as error:
        return [f"не удалось загрузить KB: {type(error).__name__}: {error}"]

    for field_name in REQUIRED_COMPANY_FIELDS:
        if not str(company.get(field_name) or "").strip():
            errors.append(f"company.yaml: пустое или отсутствует поле {field_name}")

    company_id = str(company.get("company_id") or "").strip()
    if not company_id:
        errors.append("company.yaml: company_id пустой")

    if kb_dir.name != company_id:
        errors.append(f"company_id должен совпадать с папкой: {company_id!r} != {kb_dir.name!r}")

    service_ids = [str(service.get("id") or "").strip() for service in services]
    duplicate_service_ids = sorted({service_id for service_id in service_ids if service_ids.count(service_id) > 1})
    if duplicate_service_ids:
        errors.append("дубли service.id: " + ", ".join(duplicate_service_ids))

    if not services:
        errors.append("services.json: нужен хотя бы один service")

    service_id_set = set(service_ids)
    for index, service in enumerate(services, start=1):
        service_id = str(service.get("id") or "").strip()
        for field_name in REQUIRED_SERVICE_FIELDS:
            if field_name not in service:
                errors.append(f"services.json[{index}]: нет поля {field_name}")
        if not service_id:
            errors.append(f"services.json[{index}]: пустой id")
        if not str(service.get("name") or "").strip():
            errors.append(f"services.json[{index}]: пустое name у service_id={service_id!r}")
        if not str(service.get("short_description") or "").strip():
            errors.append(f"services.json[{index}]: пустое short_description у service_id={service_id!r}")
        if not isinstance(service.get("synonyms"), list):
            errors.append(f"services.json[{index}]: synonyms должен быть массивом")

    for index, price in enumerate(prices, start=1):
        service_id = str(price.get("service_id") or "").strip()
        if service_id not in service_id_set:
            errors.append(f"prices.json[{index}]: service_id не найден в services.json: {service_id!r}")
        if not str(price.get("price_text") or "").strip():
            errors.append(f"prices.json[{index}]: пустое price_text у service_id={service_id!r}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить клиентскую базу знаний.")
    parser.add_argument("kb_dir", type=Path, help="Папка KB, например backend/data/clients/rosh_demo")
    args = parser.parse_args()

    errors = validate_kb(args.kb_dir)
    if errors:
        print("KB validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"KB validation ok: {args.kb_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
