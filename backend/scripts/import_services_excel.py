"""Импортирует прайс XLSX в staging-данные без публикации в runtime KB."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zipfile import ZipFile


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

DEFAULT_INPUT = REPO_ROOT / "client-input" / "excel" / "Прайс-лист 25.05.2026.xlsx"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "client-input" / "normalized" / "rosh_price"
PRICE_COLUMNS = (
    "Название услуги",
    "Цена",
    "Код услуги",
    "Длительность",
    "Рекомендации по подготовке",
    "Рекомендации после приёма",
    "Код по приказу №804н",
    "Ставка НДС",
)
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


@dataclass(frozen=True)
class ParsedRow:
    row_number: int
    row_type: str
    category: str | None
    values: dict[str, str]


def _col_to_index(cell_ref: str) -> int | None:
    match = re.match(r"([A-Z]+)", cell_ref)
    if not match:
        return None
    index = 0
    for char in match.group(1):
        index = index * 26 + ord(char) - 64
    return index - 1


def _load_shared_strings(archive: ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []

    values: list[str] = []
    for item in root.findall("main:si", NS):
        parts = [text_node.text or "" for text_node in item.findall(".//main:t", NS)]
        values.append("".join(parts))
    return values


def _cell_value(cell: ET.Element, shared_strings: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(text_node.text or "" for text_node in cell.findall(".//main:t", NS)).strip()

    value_node = cell.find("main:v", NS)
    if value_node is None:
        return ""

    raw_value = value_node.text or ""
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)].strip()
        except (IndexError, ValueError):
            return raw_value.strip()
    if cell_type == "b":
        return "true" if raw_value == "1" else "false"
    return raw_value.strip()


def _xlsx_rows(path: Path, sheet_name: str | None = None) -> tuple[str, list[list[str]]]:
    with ZipFile(path) as archive:
        shared_strings = _load_shared_strings(archive)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationship_map = {
            relationship.attrib["Id"]: relationship.attrib["Target"]
            for relationship in relationships.findall("pkgrel:Relationship", NS)
        }

        sheets = workbook.findall("main:sheets/main:sheet", NS)
        selected_sheet = None
        for sheet in sheets:
            if sheet_name is None or sheet.attrib.get("name") == sheet_name:
                selected_sheet = sheet
                break
        if selected_sheet is None:
            available = ", ".join(sheet.attrib.get("name", "") for sheet in sheets)
            raise ValueError(f"sheet not found: {sheet_name!r}; available: {available}")

        selected_name = selected_sheet.attrib.get("name") or "Sheet1"
        relationship_id = selected_sheet.attrib.get(f"{{{NS['rel']}}}id")
        target = relationship_map.get(str(relationship_id), "")
        sheet_path = "xl/" + target.lstrip("/")
        if sheet_path not in archive.namelist():
            sheet_path = "xl/worksheets/" + Path(target).name

        sheet_root = ET.fromstring(archive.read(sheet_path))
        rows: list[list[str]] = []
        for row in sheet_root.findall("main:sheetData/main:row", NS):
            values: list[str] = []
            for cell in row.findall("main:c", NS):
                index = _col_to_index(cell.attrib.get("r", ""))
                while index is not None and len(values) < index:
                    values.append("")
                values.append(_cell_value(cell, shared_strings))
            rows.append(values)
        return selected_name, rows


def _normalize_header(row: list[str]) -> list[str]:
    headers = [str(value or "").strip() for value in row]
    if not any(headers):
        raise ValueError("header row is empty")
    return [header or f"column_{index + 1}" for index, header in enumerate(headers)]


def _row_values(headers: list[str], row: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, header in enumerate(headers):
        values[header] = str(row[index]).strip() if index < len(row) else ""
    return values


def _non_empty_values(values: dict[str, str]) -> list[str]:
    return [value for value in values.values() if value.strip()]


def _parse_rows(rows: list[list[str]]) -> tuple[list[str], list[ParsedRow]]:
    if not rows:
        raise ValueError("empty workbook")
    headers = _normalize_header(rows[0])
    parsed: list[ParsedRow] = []
    current_category: str | None = None

    for row_index, row in enumerate(rows[1:], start=2):
        values = _row_values(headers, row)
        name = values.get("Название услуги", "").strip()
        price = values.get("Цена", "").strip()
        non_empty = _non_empty_values(values)
        if not non_empty:
            continue

        if name and not price and len(non_empty) == 1:
            current_category = name
            parsed.append(
                ParsedRow(
                    row_number=row_index,
                    row_type="category",
                    category=current_category,
                    values=values,
                )
            )
            continue

        row_type = "service_price" if name and price else "unknown"
        parsed.append(
            ParsedRow(
                row_number=row_index,
                row_type=row_type,
                category=current_category,
                values=values,
            )
        )

    return headers, parsed


def _slugify(value: str) -> str:
    value = value.lower()
    transliterated = "".join(TRANSLIT.get(char, char) for char in value)
    transliterated = re.sub(r"[^a-z0-9]+", "_", transliterated)
    transliterated = re.sub(r"_+", "_", transliterated).strip("_")
    return transliterated or "service"


def _stable_service_id(company_id: str, category: str, name: str) -> str:
    digest = hashlib.sha1(f"{company_id}|{category}|{name}".encode("utf-8")).hexdigest()[:8]
    slug = _slugify(name)[:56].strip("_")
    return f"{slug}_{digest}"


def _format_price(value: str) -> tuple[str, int | None]:
    clean = value.replace("\u00a0", "").replace(" ", "").replace(",", ".").strip()
    try:
        amount = Decimal(clean)
    except InvalidOperation:
        return value.strip(), None

    if amount == amount.to_integral():
        integer_amount = int(amount)
        return f"{integer_amount:,}".replace(",", " ") + " ₽", integer_amount
    return f"{amount.normalize()} ₽", None


def _build_outputs(company_id: str, parsed_rows: list[ParsedRow]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    raw_rows: list[dict[str, Any]] = []
    services: list[dict[str, Any]] = []
    prices: list[dict[str, Any]] = []

    for parsed in parsed_rows:
        raw_payload = {
            "row_number": parsed.row_number,
            "row_type": parsed.row_type,
            "category": parsed.category,
            "values": parsed.values,
        }
        raw_rows.append(raw_payload)

        if parsed.row_type != "service_price":
            continue

        name = parsed.values.get("Название услуги", "").strip()
        category = str(parsed.category or "Без категории").strip()
        service_id = _stable_service_id(company_id, category, name)
        price_text, price_value = _format_price(parsed.values.get("Цена", ""))
        duration = parsed.values.get("Длительность", "").strip() or None
        preparation = parsed.values.get("Рекомендации по подготовке", "").strip()
        aftercare = parsed.values.get("Рекомендации после приёма", "").strip()
        service_code = parsed.values.get("Код услуги", "").strip()
        order_code = parsed.values.get("Код по приказу №804н", "").strip()
        vat = parsed.values.get("Ставка НДС", "").strip()

        service_metadata = {
            "source": "price_list_xlsx",
            "source_row": parsed.row_number,
            "raw": parsed.values,
            "internal_code": service_code,
            "order_804n_code": order_code,
            "vat_rate": vat,
            "preparation_recommendations": preparation,
            "aftercare_recommendations": aftercare,
        }

        services.append(
            {
                "id": service_id,
                "name": name,
                "category": category,
                "synonyms": [],
                "short_description": name,
                "price_from": price_value,
                "duration": duration,
                "requires_specialist": True,
                "source_note": f"Прайс-лист 25.05.2026, строка {parsed.row_number}",
                "page_url": None,
                "public": False,
                "metadata": service_metadata,
            }
        )
        prices.append(
            {
                "service_id": service_id,
                "price_text": price_text,
                "comment": "Стоимость из прайс-листа 25.05.2026. Точную стоимость подтверждает специалист.",
                "metadata": {
                    "source": "price_list_xlsx",
                    "source_row": parsed.row_number,
                    "raw_price": parsed.values.get("Цена", ""),
                    "price_value": price_value,
                    "internal_code": service_code,
                    "order_804n_code": order_code,
                    "vat_rate": vat,
                },
            }
        )

    return raw_rows, services, prices


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def _build_report(
    *,
    input_path: Path,
    sheet_name: str,
    headers: list[str],
    parsed_rows: list[ParsedRow],
    services: list[dict[str, Any]],
    prices: list[dict[str, Any]],
) -> str:
    category_counts = Counter(service["category"] for service in services)
    unknown_rows = [row for row in parsed_rows if row.row_type == "unknown"]
    missing_duration = sum(1 for service in services if not service.get("duration"))
    missing_preparation = sum(
        1 for service in services if not service.get("metadata", {}).get("preparation_recommendations")
    )
    missing_aftercare = sum(1 for service in services if not service.get("metadata", {}).get("aftercare_recommendations"))

    lines = [
        "# Price Import Report",
        "",
        f"- input: `{input_path}`",
        f"- sheet: `{sheet_name}`",
        f"- headers: {', '.join(headers)}",
        f"- parsed rows: {len(parsed_rows)}",
        f"- service rows: {len(services)}",
        f"- price rows: {len(prices)}",
        f"- category rows: {sum(1 for row in parsed_rows if row.row_type == 'category')}",
        f"- unknown rows: {len(unknown_rows)}",
        "",
        "## Data Completeness",
        "",
        f"- services without duration: {missing_duration}",
        f"- services without preparation recommendations: {missing_preparation}",
        f"- services without aftercare recommendations: {missing_aftercare}",
        "",
        "## Top Categories",
        "",
    ]
    for category, count in category_counts.most_common(20):
        lines.append(f"- {count}: {category}")
    if unknown_rows:
        lines.extend(["", "## Unknown Rows", ""])
        for row in unknown_rows[:50]:
            compact = {key: value for key, value in row.values.items() if value}
            lines.append(f"- row {row.row_number}: {compact}")
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Runtime KB не изменялась.",
            "- Все исходные колонки сохранены в `raw_price_rows.jsonl` и `metadata.raw`.",
            "- Поля `internal_code`, `order_804n_code`, `vat_rate` сохранены как metadata и не должны показываться пользователю по умолчанию.",
            "- `public=false` у всех услуг: перед публикацией нужен whitelist категорий/услуг.",
        ]
    )
    return "\n".join(lines) + "\n"


def _print_summary(parsed_rows: list[ParsedRow], services: list[dict[str, Any]], prices: list[dict[str, Any]]) -> None:
    category_counts = Counter(service["category"] for service in services)
    print("Price import staging")
    print("────────────────────")
    print(f"parsed rows: {len(parsed_rows)}")
    print(f"services: {len(services)}")
    print(f"prices: {len(prices)}")
    print(f"categories: {len(category_counts)}")
    print("top categories:")
    for category, count in category_counts.most_common(10):
        print(f"  {count:>3}  {category}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Импортировать прайс XLSX в staging-файлы без публикации KB.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Путь к XLSX прайсу")
    parser.add_argument("--sheet", default=None, help="Название листа. По умолчанию первый лист")
    parser.add_argument("--company", default="rosh_demo", help="company_id для stable service_id")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Куда писать staging-файлы")
    parser.add_argument("--dry-run", action="store_true", help="Показать сводку без записи файлов")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input.resolve()
    if not input_path.exists():
        print(f"Input not found: {input_path}", file=sys.stderr)
        return 1

    try:
        sheet_name, rows = _xlsx_rows(input_path, args.sheet)
        headers, parsed_rows = _parse_rows(rows)
        raw_rows, services, prices = _build_outputs(args.company, parsed_rows)
    except Exception as error:
        print(f"Import failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    _print_summary(parsed_rows, services, prices)
    if args.dry_run:
        print("dry-run: files were not written")
        return 0

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(output_dir / "raw_price_rows.jsonl", raw_rows)
    _write_json(output_dir / "services_staging.json", services)
    _write_json(output_dir / "prices_staging.json", prices)
    (output_dir / "import_report.md").write_text(
        _build_report(
            input_path=input_path,
            sheet_name=sheet_name,
            headers=headers,
            parsed_rows=parsed_rows,
            services=services,
            prices=prices,
        ),
        encoding="utf-8",
    )
    print("")
    print(f"written: {output_dir / 'raw_price_rows.jsonl'}")
    print(f"written: {output_dir / 'services_staging.json'}")
    print(f"written: {output_dir / 'prices_staging.json'}")
    print(f"written: {output_dir / 'import_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
