"""Добавляет новые service groups в УЖЕ существующие services.json/prices.json клиента,
не трогая ничего остального (company.yaml/config.yaml/faq.md — там ручной тюнинг).

В отличие от publish_rosh_import_demo.py (полная пересборка клиента с нуля), этот скрипт
только ДОБАВЛЯЕТ группы — безопасно гонять инкрементально по мере разбора прайса.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_CLIENTS_DIR = BACKEND_DIR / "data" / "clients"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _price_comment(group: dict[str, Any]) -> str:
    variants_count = len(group.get("variants") if isinstance(group.get("variants"), list) else [])
    if variants_count > 1:
        return (
            f"В направлении {variants_count} вариантов из прайса. "
            "Стоимость зависит от зоны/объема/препарата, точную сумму подтвердит специалист."
        )
    return "Стоимость из прайс-листа. Точную сумму подтвердит специалист."


def _service_from_group(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": group["id"],
        "name": group["name"],
        "category": group["category"],
        "synonyms": group.get("synonyms") or [],
        "short_description": group["short_description"],
        "price_from": group.get("price_from"),
        "price_to": group.get("price_to"),
        "price_range_text": group.get("price_range_text"),
        "duration": None,
        "requires_specialist": True,
        "source_note": group.get("source_note"),
        "page_url": group.get("page_url"),
        "variants": group.get("variants") or [],
    }


def _price_from_group(group: dict[str, Any]) -> dict[str, Any]:
    return {
        "service_id": group["id"],
        "price_text": group.get("price_range_text") or "Цена зависит от параметров услуги",
        "comment": _price_comment(group),
    }


def merge(company_id: str, groups_file: Path, clients_dir: Path) -> tuple[int, int]:
    groups = _load_json(groups_file)
    if not isinstance(groups, list) or not groups:
        raise ValueError("groups file must contain non-empty list")

    target_dir = clients_dir / company_id
    services_path = target_dir / "services.json"
    prices_path = target_dir / "prices.json"
    services = _load_json(services_path)
    prices = _load_json(prices_path)

    existing_service_ids = {str(item.get("id")) for item in services if isinstance(item, dict)}
    existing_price_ids = {str(item.get("service_id")) for item in prices if isinstance(item, dict)}

    added_services = 0
    added_prices = 0
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_id = str(group.get("id") or "")
        if group_id in existing_service_ids:
            print(f"skip (already exists): {group_id} — {group.get('name')}")
            continue
        services.append(_service_from_group(group))
        added_services += 1
        if group_id not in existing_price_ids:
            prices.append(_price_from_group(group))
            added_prices += 1

    _write_json(services_path, services)
    _write_json(prices_path, prices)
    return added_services, added_prices


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Добавить новые service groups в существующего клиента.")
    parser.add_argument("--company", required=True)
    parser.add_argument("--groups-file", type=Path, required=True)
    parser.add_argument("--clients-dir", type=Path, default=DEFAULT_CLIENTS_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    added_services, added_prices = merge(args.company, args.groups_file.resolve(), args.clients_dir.resolve())
    print(f"added services: {added_services}")
    print(f"added prices: {added_prices}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
