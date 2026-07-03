"""Публикует локального demo-клиента из импортированных групп услуг."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
DEFAULT_GROUPS_FILE = REPO_ROOT / "client-input" / "normalized" / "rosh_price" / "service_groups_with_urls.json"
DEFAULT_CLIENTS_DIR = BACKEND_DIR / "data" / "clients"
DEFAULT_COMPANY_ID = "rosh_import_demo"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _yaml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _dump_yaml(payload: dict[str, Any], indent: int = 0) -> str:
    lines: list[str] = []
    prefix = " " * indent
    for key, value in payload.items():
        if isinstance(value, dict):
            lines.append(f"{prefix}{key}:")
            lines.append(_dump_yaml(value, indent + 2).rstrip())
        elif isinstance(value, list):
            lines.append(f"{prefix}{key}:")
            for item in value:
                if isinstance(item, dict):
                    lines.append(f"{prefix}  -")
                    lines.append(_dump_yaml(item, indent + 4).rstrip())
                else:
                    lines.append(f"{prefix}  - {_yaml_scalar(item)}")
        else:
            lines.append(f"{prefix}{key}: {_yaml_scalar(value)}")
    return "\n".join(lines) + "\n"


def _price_comment(group: dict[str, Any]) -> str:
    variants_count = len(group.get("variants") if isinstance(group.get("variants"), list) else [])
    if variants_count > 1:
        return (
            f"В направлении {variants_count} позиций прайса. "
            "Стоимость зависит от зоны/объема/препарата, точную сумму подтвердит специалист."
        )
    return "Стоимость из прайс-листа 25.05.2026. Точную сумму подтвердит специалист."


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


def _company_yaml(company_id: str) -> dict[str, Any]:
    return {
        "company_id": company_id,
        "company_name": "Медицинский центр РОШ",
        "city": "Москва",
        "working_hours": "Пн-Пт 9:00-20:00, Сб 10:00-18:00",
        "phone": "+7 (495) 000-00-00",
        "address": "Москва, адрес уточняется оператором",
        "website_url": "https://www.medcenterrosh.ru/",
        "telegram_url": "https://t.me/rosh_demo",
        "allowed_domains": ["localhost", "medcenterrosh.ru", "www.medcenterrosh.ru"],
        "allowed_topics": [
            "услуги центра",
            "цены",
            "запись",
            "ссылки на страницы услуг",
            "адрес и режим работы",
        ],
        "operator_triggers": ["оператор", "менеджер", "живой человек", "администратор", "специалист", "человек"],
        "forbidden_claims": [
            "диагнозы",
            "назначение препаратов",
            "гарантии результата",
            "медицинские рекомендации",
            "услуги не из базы",
        ],
        "safety_disclaimer": "Я не врач и не ставлю диагнозы. По медицинским вопросам вас сориентирует специалист.",
    }


def _config_yaml() -> dict[str, Any]:
    return {
        "domain_profile": {
            "type": "medical",
            "restricted_advice": ["medical", "diagnosis", "treatment"],
        },
        "features": {
            "operator": True,
            "lead_capture": True,
            "analytics": False,
            "site_navigation_links": True,
        },
        "widget": {
            "primary_color": "#1F7A5C",
            "button_color": "#1F7A5C",
            "header_title": "Медицинский центр РОШ",
            "header_subtitle": "Подскажем по услугам и ценам",
            "position": "bottom-right",
            "avatar_emoji": "💬",
        },
        "phrasebook": {
            "company_type": "медицинский центр",
            "specialist_name": "специалист",
            "price_disclaimer": "Предварительно так, точнее подскажет специалист после уточнения деталей.",
            "unknown_service": "В базе такой услуги не вижу. Могу показать список услуг или передать вопрос специалисту.",
            "contact_prompt": "Оставьте имя и телефон, и специалист сможет связаться с вами позже.",
        },
        "fact_guards": [
            {
                "topic": "ботулинотерапия",
                "service_id": "botulinoterapiya_9d5734af",
                "known_values": ["Ксеомин", "Миотокс"],
                "blocked_values": ["Ботокс", "Диспорт", "Релатокс", "Нейронокс", "Лантокс"],
            }
        ],
    }


def _faq_md(groups: list[dict[str, Any]]) -> str:
    group_names = ", ".join(str(group.get("name") or "") for group in groups[:12])
    return f"""# FAQ

## Услуги

В базе тестового клиента опубликованы направления из прайса: {group_names}.

## Цены

Цены взяты из прайс-листа от 25.05.2026 и сгруппированы по направлениям. Для точной суммы специалист уточнит зону, объем, препарат или формат услуги.

## Запись

Для заявки пользователь оставляет имя и телефон. Специалист подтверждает запись и детали услуги.

## Медицинские вопросы

Виджет не ставит диагнозы, не назначает лечение и не дает индивидуальные медицинские рекомендации. Такие вопросы передаются специалисту.
"""


def publish(company_id: str, groups_file: Path, clients_dir: Path, *, force: bool) -> Path:
    groups = _load_json(groups_file)
    if not isinstance(groups, list) or not groups:
        raise ValueError("groups file must contain non-empty list")

    target_dir = clients_dir / company_id
    if target_dir.exists():
        if not force:
            raise FileExistsError(f"{target_dir} already exists; use --force")
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    services = [_service_from_group(group) for group in groups if isinstance(group, dict)]
    prices = [_price_from_group(group) for group in groups if isinstance(group, dict)]
    _write_json(target_dir / "services.json", services)
    _write_json(target_dir / "prices.json", prices)
    (target_dir / "company.yaml").write_text(_dump_yaml(_company_yaml(company_id)), encoding="utf-8")
    (target_dir / "config.yaml").write_text(_dump_yaml(_config_yaml()), encoding="utf-8")
    (target_dir / "faq.md").write_text(_faq_md(groups), encoding="utf-8")
    return target_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Опубликовать локального rosh_import_demo клиента.")
    parser.add_argument("--company", default=DEFAULT_COMPANY_ID)
    parser.add_argument("--groups-file", type=Path, default=DEFAULT_GROUPS_FILE)
    parser.add_argument("--clients-dir", type=Path, default=DEFAULT_CLIENTS_DIR)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        target_dir = publish(
            company_id=args.company,
            groups_file=args.groups_file.resolve(),
            clients_dir=args.clients_dir.resolve(),
            force=args.force,
        )
    except Exception as error:
        print(f"Publish failed: {type(error).__name__}: {error}")
        return 1

    services = _load_json(target_dir / "services.json")
    print(f"Published demo client: {target_dir}")
    print(f"services: {len(services)}")
    print(f"prices: {len(_load_json(target_dir / 'prices.json'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
