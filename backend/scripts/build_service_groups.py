"""Собирает публичные услуги в направления с вариантами из прайса."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
DEFAULT_STAGING_DIR = REPO_ROOT / "client-input" / "normalized" / "rosh_price"

TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "i",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "c",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}
SYNONYM_HINTS = {
    "Ботулинотерапия": ["ботулинотерапия", "ксеомин", "миотокс"],
    "Биоревитализация": ["биоревитализация", "уколы красоты", "увлажнение кожи"],
    "Игольчатый RF лифтинг": ["rf лифтинг", "рф лифтинг", "игольчатый rf"],
    "Консультации": ["консультация", "прием врача", "приём врача", "врач"],
    "Лазерная эпиляция": ["лазерная эпиляция", "эпиляция", "удаление волос лазером"],
    "Мезотерапия": ["мезотерапия", "мезо"],
    "Пилинги": ["пилинг", "пилинги"],
    "Филлеры": ["филлеры", "контурная пластика"],
    "Фотолечение BBL": ["bbl", "фототерапия", "фотолечение"],
    "Чистки": ["чистка лица", "чистка", "механическая чистка"],
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _slugify(value: str) -> str:
    value = value.lower()
    transliterated = "".join(TRANSLIT.get(char, char) for char in value)
    transliterated = re.sub(r"[^a-z0-9]+", "_", transliterated)
    transliterated = re.sub(r"_+", "_", transliterated).strip("_")
    return transliterated or "service_group"


def _group_id(company_id: str, category: str) -> str:
    digest = hashlib.sha1(f"{company_id}|{category}".encode("utf-8")).hexdigest()[:8]
    return f"{_slugify(category)[:48]}_{digest}"


def _price_range(values: list[int]) -> tuple[int | None, int | None, str | None]:
    if not values:
        return None, None, None
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        return minimum, maximum, f"{minimum:,}".replace(",", " ") + " ₽"
    return minimum, maximum, f"от {minimum:,} до {maximum:,}".replace(",", " ") + " ₽"


def _build_groups(company_id: str, services: list[dict[str, Any]], prices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    price_by_service = {
        str(price.get("service_id") or ""): price
        for price in prices
        if isinstance(price, dict)
    }
    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for service in services:
        if isinstance(service, dict):
            by_category[str(service.get("category") or "Без категории")].append(service)

    groups: list[dict[str, Any]] = []
    for category, items in sorted(by_category.items()):
        price_values = [
            int(service["price_from"])
            for service in items
            if isinstance(service.get("price_from"), int) and int(service["price_from"]) > 0
        ]
        minimum, maximum, range_text = _price_range(price_values)
        variants = []
        for service in sorted(items, key=lambda item: str(item.get("name") or "")):
            service_id = str(service.get("id") or "")
            price = price_by_service.get(service_id) or {}
            variants.append(
                {
                    "source_service_id": service_id,
                    "name": service.get("name"),
                    "price_text": price.get("price_text"),
                    "price_from": service.get("price_from"),
                    "duration": service.get("duration"),
                    "source_note": service.get("source_note"),
                }
            )

        groups.append(
            {
                "id": _group_id(company_id, category),
                "name": category,
                "category": category,
                "synonyms": SYNONYM_HINTS.get(category, []),
                "short_description": f"Направление «{category}». В прайсе {len(variants)} вариантов.",
                "price_from": minimum,
                "price_to": maximum,
                "price_range_text": range_text,
                "requires_specialist": True,
                "source_note": "Сгруппировано из прайс-листа 25.05.2026",
                "variants": variants,
            }
        )
    return groups


def _build_report(groups: list[dict[str, Any]]) -> str:
    lines = [
        "# Service Groups Report",
        "",
        f"- groups: {len(groups)}",
        f"- source positions: {sum(len(group['variants']) for group in groups)}",
        "",
        "## Groups",
        "",
    ]
    for group in groups:
        lines.append(
            f"- {group['name']}: {len(group['variants'])} вариантов, {group.get('price_range_text') or 'цена не указана'}"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Это preview-слой для согласования, runtime KB пока не изменялась.",
            "- Пользователю можно показывать groups, а конкретные variants использовать для уточнения цены.",
            "- Перед публикацией нужны synonyms и page_url для ключевых групп.",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Собрать service groups из публичного candidate-набора.")
    parser.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING_DIR)
    parser.add_argument("--company", default="rosh_demo")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--source",
        choices=["candidate", "staging"],
        default="candidate",
        help=(
            "candidate — только уже отобранный public-слой (services_candidate.json, поведение "
            "по умолчанию, как раньше); staging — полный набор из прайса (services_staging.json), "
            "нужен вместе с --categories, чтобы точечно вытащить direct_search-категории."
        ),
    )
    parser.add_argument(
        "--categories",
        default=None,
        help="Список категорий через запятую (только с --source staging) — точечно добавить, а не весь прайс.",
    )
    parser.add_argument(
        "--output-suffix",
        default="",
        help="Суффикс для выходных файлов (например '_batch2'), чтобы не перезаписать candidate-набор.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    staging_dir = args.staging_dir.resolve()

    services_file = "services_candidate.json" if args.source == "candidate" else "services_staging.json"
    prices_file = "prices_candidate.json" if args.source == "candidate" else "prices_staging.json"
    services = _load_json(staging_dir / services_file)
    prices = _load_json(staging_dir / prices_file)
    if not isinstance(services, list) or not isinstance(prices, list):
        raise SystemExit("services/prices must be lists")

    if args.categories:
        wanted = {item.strip() for item in args.categories.split(",") if item.strip()}
        services = [s for s in services if isinstance(s, dict) and str(s.get("category") or "") in wanted]
        missing = wanted - {str(s.get("category") or "") for s in services}
        if missing:
            raise SystemExit(f"категории не найдены в {services_file}: {sorted(missing)}")

    groups = _build_groups(args.company, services, prices)
    print("Service groups")
    print("──────────────")
    print(f"groups: {len(groups)}")
    print(f"source positions: {sum(len(group['variants']) for group in groups)}")
    for group in groups:
        print(f"  {len(group['variants']):>3}  {group['name']} — {group.get('price_range_text')}")

    if args.dry_run:
        print("dry-run: files were not written")
        return 0

    groups_path = staging_dir / f"service_groups_candidate{args.output_suffix}.json"
    report_path = staging_dir / f"service_groups_report{args.output_suffix}.md"
    _write_json(groups_path, groups)
    report_path.write_text(_build_report(groups), encoding="utf-8")
    print("")
    print(f"written: {groups_path}")
    print(f"written: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
