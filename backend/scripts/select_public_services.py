"""Фильтрует staging-услуги в публичный набор для виджета."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

DEFAULT_STAGING_DIR = REPO_ROOT / "client-input" / "normalized" / "rosh_price"
DEFAULT_RULES_FILE = DEFAULT_STAGING_DIR / "public_rules.json"
VALID_MODES = {"public", "direct_search", "hidden"}
DEFAULT_PUBLIC_PATTERNS = (
    "консульта",
    "лазер",
    "эпиляц",
    "rf",
    "bbl",
    "halo",
    "forever young",
    "skin tyte",
    "биоревитал",
    "мезотерап",
    "пилинг",
    "филлер",
    "ботулин",
    "уход",
    "маск",
    "чист",
)
DEFAULT_HIDDEN_PATTERNS = (
    "(lq)",
    "анализ",
    "исследование крови",
    "гормон",
    "инфекции",
    "аутоантитела",
    "без категории",
)
DEFAULT_DIRECT_SEARCH_PATTERNS = (
    "узи",
    "гинеколог",
    "удаление новообраз",
    "bicom",
    "инъекц",
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _category_mode(category: str) -> str:
    normalized = category.lower()
    if any(pattern in normalized for pattern in DEFAULT_HIDDEN_PATTERNS):
        return "hidden"
    if any(pattern in normalized for pattern in DEFAULT_PUBLIC_PATTERNS):
        return "public"
    if any(pattern in normalized for pattern in DEFAULT_DIRECT_SEARCH_PATTERNS):
        return "direct_search"
    return "direct_search"


def _init_rules(staging_dir: Path, rules_file: Path) -> None:
    services = _load_json(staging_dir / "services_staging.json")
    if not isinstance(services, list):
        raise ValueError("services_staging.json must be a list")

    categories = sorted({str(service.get("category") or "Без категории") for service in services})
    category_modes = {category: _category_mode(category) for category in categories}
    payload = {
        "description": "Rules for selecting which imported services are visible in widget.",
        "default_mode": "hidden",
        "modes": {
            "public": "show in service list and answer normally",
            "direct_search": "keep for future exact search, do not include in public runtime KB yet",
            "hidden": "do not use in widget without manual approval",
        },
        "category_modes": category_modes,
        "service_overrides": {},
        "name_exclude_patterns": [],
        "name_include_patterns": [],
    }
    rules_file.parent.mkdir(parents=True, exist_ok=True)
    _write_json(rules_file, payload)


def _compile_patterns(patterns: list[Any]) -> list[re.Pattern[str]]:
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        if not str(pattern).strip():
            continue
        compiled.append(re.compile(str(pattern), re.IGNORECASE))
    return compiled


def _service_mode(service: dict[str, Any], rules: dict[str, Any]) -> str:
    service_id = str(service.get("id") or "")
    name = str(service.get("name") or "")
    category = str(service.get("category") or "Без категории")
    service_overrides = rules.get("service_overrides")
    category_modes = rules.get("category_modes")
    default_mode = str(rules.get("default_mode") or "hidden")

    if isinstance(service_overrides, dict) and service_id in service_overrides:
        mode = str(service_overrides[service_id])
    elif isinstance(category_modes, dict) and category in category_modes:
        mode = str(category_modes[category])
    else:
        mode = default_mode

    include_patterns = _compile_patterns(rules.get("name_include_patterns") if isinstance(rules.get("name_include_patterns"), list) else [])
    exclude_patterns = _compile_patterns(rules.get("name_exclude_patterns") if isinstance(rules.get("name_exclude_patterns"), list) else [])

    if any(pattern.search(name) for pattern in include_patterns):
        mode = "public"
    if any(pattern.search(name) for pattern in exclude_patterns):
        mode = "hidden"

    if mode not in VALID_MODES:
        return default_mode if default_mode in VALID_MODES else "hidden"
    return mode


def _strip_runtime_service(service: dict[str, Any], *, public_mode: str) -> dict[str, Any]:
    allowed_keys = {
        "id",
        "name",
        "category",
        "synonyms",
        "short_description",
        "price_from",
        "duration",
        "requires_specialist",
        "source_note",
        "page_url",
    }
    payload = {key: value for key, value in service.items() if key in allowed_keys}
    payload["public_mode"] = public_mode
    return payload


def _strip_runtime_price(price: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {"service_id", "price_text", "comment"}
    return {key: value for key, value in price.items() if key in allowed_keys}


def _build_report(
    *,
    services: list[dict[str, Any]],
    selected_by_mode: dict[str, list[dict[str, Any]]],
    rules_file: Path,
) -> str:
    lines = [
        "# Public Service Selection Report",
        "",
        f"- rules: `{rules_file}`",
        f"- total staging services: {len(services)}",
        f"- public services: {len(selected_by_mode['public'])}",
        f"- direct_search services: {len(selected_by_mode['direct_search'])}",
        f"- hidden services: {len(selected_by_mode['hidden'])}",
        "",
        "## Mode Distribution By Category",
        "",
    ]

    by_category: dict[str, Counter[str]] = defaultdict(Counter)
    for mode, items in selected_by_mode.items():
        for service in items:
            by_category[str(service.get("category") or "Без категории")][mode] += 1

    for category, counter in sorted(by_category.items()):
        parts = ", ".join(f"{mode}: {count}" for mode, count in sorted(counter.items()))
        lines.append(f"- {category}: {parts}")

    lines.extend(["", "## Public Categories", ""])
    public_categories = Counter(str(service.get("category") or "Без категории") for service in selected_by_mode["public"])
    for category, count in public_categories.most_common():
        lines.append(f"- {count}: {category}")

    lines.extend(["", "## Notes", ""])
    lines.append("- Runtime KB не изменялась.")
    lines.append("- `services_public_candidate.json` содержит только `public` услуги.")
    lines.append("- `direct_search` пока не публикуется в runtime KB, чтобы не раздувать список услуг.")
    lines.append("- Перед публикацией проверить категории и при необходимости вручную поправить `public_rules.json`.")
    return "\n".join(lines) + "\n"


def _run_selection(staging_dir: Path, rules_file: Path, *, dry_run: bool, include_direct_search: bool) -> None:
    services = _load_json(staging_dir / "services_staging.json")
    prices = _load_json(staging_dir / "prices_staging.json")
    rules = _load_json(rules_file)
    if not isinstance(services, list) or not isinstance(prices, list):
        raise ValueError("staging services/prices must be lists")
    if not isinstance(rules, dict):
        raise ValueError("rules must be an object")

    selected_by_mode: dict[str, list[dict[str, Any]]] = {mode: [] for mode in VALID_MODES}
    for service in services:
        if not isinstance(service, dict):
            continue
        mode = _service_mode(service, rules)
        selected_by_mode[mode].append(service)

    public_ids = {str(service.get("id") or "") for service in selected_by_mode["public"]}
    direct_ids = {str(service.get("id") or "") for service in selected_by_mode["direct_search"]}
    public_services = [_strip_runtime_service(service, public_mode="public") for service in selected_by_mode["public"]]
    direct_services = [
        _strip_runtime_service(service, public_mode="direct_search")
        for service in selected_by_mode["direct_search"]
    ]
    public_prices = [
        _strip_runtime_price(price)
        for price in prices
        if isinstance(price, dict) and str(price.get("service_id") or "") in public_ids
    ]
    direct_prices = [
        _strip_runtime_price(price)
        for price in prices
        if isinstance(price, dict) and str(price.get("service_id") or "") in direct_ids
    ]

    print("Public service selection")
    print("────────────────────────")
    print(f"staging services: {len(services)}")
    print(f"public: {len(public_services)}")
    print(f"direct_search: {len(direct_services)}")
    print(f"hidden: {len(selected_by_mode['hidden'])}")
    print("public categories:")
    public_categories = Counter(str(service.get("category") or "Без категории") for service in public_services)
    for category, count in public_categories.most_common(15):
        print(f"  {count:>3}  {category}")

    if dry_run:
        print("dry-run: files were not written")
        return

    _write_json(staging_dir / "services_candidate.json", public_services)
    _write_json(staging_dir / "prices_candidate.json", public_prices)
    if include_direct_search:
        _write_json(staging_dir / "services_direct_search_candidate.json", direct_services)
        _write_json(staging_dir / "prices_direct_search_candidate.json", direct_prices)
    (staging_dir / "selection_report.md").write_text(
        _build_report(services=services, selected_by_mode=selected_by_mode, rules_file=rules_file),
        encoding="utf-8",
    )
    print("")
    print(f"written: {staging_dir / 'services_candidate.json'}")
    print(f"written: {staging_dir / 'prices_candidate.json'}")
    if include_direct_search:
        print(f"written: {staging_dir / 'services_direct_search_candidate.json'}")
        print(f"written: {staging_dir / 'prices_direct_search_candidate.json'}")
    print(f"written: {staging_dir / 'selection_report.md'}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Выбрать публичные услуги из staging-импорта.")
    parser.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING_DIR)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULES_FILE)
    parser.add_argument("--init-rules", action="store_true", help="Создать public_rules.json по категориям")
    parser.add_argument("--dry-run", action="store_true", help="Показать сводку без записи результатов")
    parser.add_argument("--include-direct-search", action="store_true", help="Дополнительно записать direct_search candidate-файлы")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    staging_dir = args.staging_dir.resolve()
    rules_file = args.rules.resolve()

    try:
        if args.init_rules:
            _init_rules(staging_dir, rules_file)
            print(f"rules written: {rules_file}")
        if not rules_file.exists():
            print(f"Rules file not found: {rules_file}. Run with --init-rules first.", file=sys.stderr)
            return 1
        _run_selection(
            staging_dir,
            rules_file,
            dry_run=args.dry_run,
            include_direct_search=args.include_direct_search,
        )
    except Exception as error:
        print(f"Selection failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
