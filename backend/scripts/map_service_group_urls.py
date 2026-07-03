"""Связывает service groups с URL страниц услуг из CSV-реестра."""

from __future__ import annotations

import argparse
import csv
import json
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
DEFAULT_STAGING_DIR = REPO_ROOT / "client-input" / "normalized" / "rosh_price"
DEFAULT_CSV_DIR = REPO_ROOT / "client-input" / "excel"
MANUAL_OVERRIDES = {
    "Биоревитализация": "https://www.medcenterrosh.ru/services/biorevitalizaciya-mezorevitalizaciya/",
    "Ботулинотерапия": "https://www.medcenterrosh.ru/services/botulinoterapiya/",
    "Внутривенный лазер Шатл Комби": "https://www.medcenterrosh.ru/services/vnutrevennyi-lazer-shatl-kombi/",
    "Игольчатый RF лифтинг": "https://www.medcenterrosh.ru/services/igolchatiy-rf-lifting/",
    "Консультации": "https://www.medcenterrosh.ru/services/konsultaciya-kosmetologa/",
    "Лазерная Терапия Skin Tyte": "https://www.medcenterrosh.ru/services/lazernaya-metodika-skin-tyte/",
    "Лазерная терапия Forever Clear": "https://www.medcenterrosh.ru/services/lazernaya-metodika-forever-clear/",
    "Лазерная терапия Forever Young": "https://www.medcenterrosh.ru/services/lazernaya-metodika-forever-young/",
    "Лазерная терапия HALO": "https://www.medcenterrosh.ru/services/lazernaya-terapiya-halo/",
    "Лазерная шлифовка": "https://www.medcenterrosh.ru/services/lazernaya-shlifovka/",
    "Лазерная эпиляция": "https://www.medcenterrosh.ru/services/lazernaya-epilyaciya/",
    "Лазерный пилинг": "https://www.medcenterrosh.ru/services/lazernyi-piling/",
    "Мезотерапия": "https://www.medcenterrosh.ru/services/mezoterapiya/",
    "Пилинги": "https://www.medcenterrosh.ru/services/dermatologicheskiy-piling/",
    "Уходы и маски": "https://www.medcenterrosh.ru/services/uhody-i-maski/",
    "Филлеры": "https://www.medcenterrosh.ru/services/fillery/",
    "Фотолечение BBL": "https://www.medcenterrosh.ru/services/bbl-procedura-fotoomolojeniya/",
    "Чистки": "https://www.medcenterrosh.ru/services/mekhanicheskaya-chistka-litsa/",
    "Эксимерный лазер": "https://www.medcenterrosh.ru/services/eksimernyi-lazer/",
}


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _normalize(value: str) -> str:
    value = value.lower()
    value = value.replace("ё", "е")
    value = re.sub(r"https?://\S+", " ", value)
    value = re.sub(r"[^a-zа-я0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _canonical_group_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip()


def _service_url_rows(csv_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(csv_dir.glob("ROSH сводная по статьям*.csv")):
        with path.open(encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            for row in reader:
                url = str(row.get("Ссылка на сайте") or "").strip()
                if "/services/" not in url:
                    continue
                title = str(
                    row.get("Тема")
                    or row.get("Статья")
                    or row.get("Новый текст")
                    or ""
                ).strip()
                if not title:
                    continue
                rows.append(
                    {
                        "title": title,
                        "url": url,
                        "source_file": path.name,
                        "normalized_title": _normalize(title),
                    }
                )
    return rows


def _best_match(group_name: str, rows: list[dict[str, str]]) -> tuple[dict[str, str] | None, float]:
    normalized_group = _normalize(group_name)
    best_row: dict[str, str] | None = None
    best_score = 0.0
    for row in rows:
        title = row["normalized_title"]
        sequence_score = SequenceMatcher(None, normalized_group, title).ratio()
        token_group = set(normalized_group.split())
        token_title = set(title.split())
        token_score = len(token_group & token_title) / max(len(token_group), 1)
        score = max(sequence_score, token_score)
        if normalized_group in title or title in normalized_group:
            score = max(score, 0.92)
        if score > best_score:
            best_score = score
            best_row = row
    return best_row, best_score


def _map_groups(groups: list[dict[str, Any]], url_rows: list[dict[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    mapped: list[dict[str, Any]] = []
    report_rows: list[dict[str, Any]] = []
    for group in groups:
        name = _canonical_group_name(str(group.get("name") or ""))
        override_url = MANUAL_OVERRIDES.get(name)
        match, score = _best_match(name, url_rows)
        selected_url = override_url or (match["url"] if match and score >= 0.68 else None)
        selected_source = "manual_override" if override_url else "auto_match" if selected_url else "none"
        payload = {**group, "page_url": selected_url}
        mapped.append(payload)
        report_rows.append(
            {
                "group": name,
                "page_url": selected_url,
                "source": selected_source,
                "auto_score": round(score, 3),
                "auto_title": match["title"] if match else None,
                "auto_url": match["url"] if match else None,
            }
        )
    return mapped, report_rows


def _build_report(report_rows: list[dict[str, Any]]) -> str:
    mapped = [row for row in report_rows if row.get("page_url")]
    missing = [row for row in report_rows if not row.get("page_url")]
    lines = [
        "# Service Group URL Mapping Report",
        "",
        f"- groups: {len(report_rows)}",
        f"- mapped: {len(mapped)}",
        f"- missing: {len(missing)}",
        "",
        "## Mapping",
        "",
    ]
    for row in report_rows:
        status = "OK" if row.get("page_url") else "MISSING"
        lines.append(f"- {status}: {row['group']} -> {row.get('page_url') or '-'} ({row['source']})")
    if missing:
        lines.extend(["", "## Missing", ""])
        for row in missing:
            lines.append(f"- {row['group']}: auto candidate {row.get('auto_title')} / {row.get('auto_url')}")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Runtime KB не изменялась.",
            "- Manual overrides лежат в `map_service_group_urls.py` и должны быть проверены человеком.",
            "- Auto match используется только если нет manual override.",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Связать service groups с URL страниц услуг.")
    parser.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING_DIR)
    parser.add_argument("--csv-dir", type=Path, default=DEFAULT_CSV_DIR)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    staging_dir = args.staging_dir.resolve()
    groups = _load_json(staging_dir / "service_groups_candidate.json")
    if not isinstance(groups, list):
        raise SystemExit("service_groups_candidate.json must be a list")
    url_rows = _service_url_rows(args.csv_dir.resolve())
    mapped, report_rows = _map_groups(groups, url_rows)

    print("Service group URL mapping")
    print("─────────────────────────")
    print(f"groups: {len(mapped)}")
    print(f"source service urls: {len(url_rows)}")
    print(f"mapped: {sum(1 for group in mapped if group.get('page_url'))}")
    print(f"missing: {sum(1 for group in mapped if not group.get('page_url'))}")
    for row in report_rows:
        print(f"  {row['group']}: {row.get('page_url') or '-'}")

    if args.dry_run:
        print("dry-run: files were not written")
        return 0

    _write_json(staging_dir / "service_groups_with_urls.json", mapped)
    (staging_dir / "service_group_url_mapping_report.md").write_text(_build_report(report_rows), encoding="utf-8")
    print("")
    print(f"written: {staging_dir / 'service_groups_with_urls.json'}")
    print(f"written: {staging_dir / 'service_group_url_mapping_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
