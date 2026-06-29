"""публикует проверенную KB клиента в рабочую папку и печатает embed."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from validate_kb import REQUIRED_FILES, load_simple_yaml, validate_kb


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CLIENTS_DIR = REPO_ROOT / "backend" / "data" / "clients"
DEFAULT_API_BASE = "https://api.example.com"
CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _resolve_target(clients_dir: Path, company_id: str) -> Path:
    if not CLIENT_ID_PATTERN.fullmatch(company_id):
        raise ValueError("company_id может содержать только латиницу, цифры, '-' и '_'")

    root = clients_dir.resolve()
    target = (root / company_id).resolve()
    if not target.is_relative_to(root):
        raise PermissionError(f"company_id выходит за пределы clients_dir: {company_id!r}")
    return target


def _company_payload(kb_dir: Path) -> dict[str, Any]:
    company = load_simple_yaml(kb_dir / "company.yaml")
    company_id = str(company.get("company_id") or "").strip()
    if not company_id:
        raise ValueError("company.yaml: пустой company_id")
    return company


def _copy_required_files(source_dir: Path, target_dir: Path, *, force: bool) -> None:
    if target_dir.exists():
        if not force:
            raise FileExistsError(f"клиент уже существует: {target_dir}. Добавь --force для перезаписи.")
        shutil.rmtree(target_dir)

    target_dir.mkdir(parents=True)
    for file_name in REQUIRED_FILES:
        shutil.copy2(source_dir / file_name, target_dir / file_name)
    optional_config = source_dir / "config.yaml"
    if optional_config.exists() and optional_config.is_file():
        shutil.copy2(optional_config, target_dir / "config.yaml")


def _embed_blocks(api_base: str, company_id: str) -> tuple[str, str]:
    normalized_api_base = api_base.rstrip("/")
    explicit = "\n".join(
        [
            "<script",
            f'  src="{normalized_api_base}/static/widget.js"',
            f'  data-company-id="{company_id}"',
            f'  data-api-base="{normalized_api_base}"',
            "  defer",
            "></script>",
        ]
    )
    autodetect = "\n".join(
        [
            "<script",
            f'  src="{normalized_api_base}/static/widget.js"',
            f'  data-api-base="{normalized_api_base}"',
            "  defer",
            "></script>",
        ]
    )
    return explicit, autodetect


def _json_list_count(path: Path) -> int:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return len(payload) if isinstance(payload, list) else 0


def _faq_entry_count(path: Path) -> int:
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            count += 1
    return count


def _print_preview(
    source_dir: Path,
    *,
    company: dict[str, Any],
    company_id: str,
    api_base: str,
) -> None:
    explicit_embed, autodetect_embed = _embed_blocks(api_base, company_id)
    allowed_domains = company.get("allowed_domains") or []

    print("Dry run: KB валидна, файлы не копировались.")
    print(f"✅ company_name: {company.get('company_name')}")
    print(f"✅ услуг: {_json_list_count(source_dir / 'services.json')}")
    print(f"✅ цен: {_json_list_count(source_dir / 'prices.json')}")
    print(f"✅ FAQ записей: {_faq_entry_count(source_dir / 'faq.md')}")
    if isinstance(allowed_domains, list) and allowed_domains:
        print("✅ allowed_domains: " + ", ".join(str(domain) for domain in allowed_domains))
    else:
        print("✅ allowed_domains: none")
    print("")
    print("📋 Explicit embed:")
    print(explicit_embed)
    print("")
    print("📋 Autodetect embed:")
    print(autodetect_embed)
    print("")
    print("Для публикации запустите без --dry-run")


def onboard_client(
    source_dir: Path,
    *,
    clients_dir: Path = DEFAULT_CLIENTS_DIR,
    api_base: str = DEFAULT_API_BASE,
    force: bool = False,
    dry_run: bool = False,
) -> Path:
    source_dir = source_dir.resolve()
    errors = validate_kb(source_dir)
    if errors:
        raise ValueError("KB validation failed:\n" + "\n".join(f"- {error}" for error in errors))

    company = _company_payload(source_dir)
    company_id = str(company["company_id"]).strip()
    target_dir = _resolve_target(clients_dir, company_id)
    if dry_run:
        _print_preview(source_dir, company=company, company_id=company_id, api_base=api_base)
        return target_dir

    _copy_required_files(source_dir, target_dir, force=force)

    target_errors = validate_kb(target_dir)
    if target_errors:
        raise ValueError("published KB validation failed:\n" + "\n".join(f"- {error}" for error in target_errors))

    explicit_embed, autodetect_embed = _embed_blocks(api_base, company_id)
    allowed_domains = company.get("allowed_domains") or []

    print(f"Client onboarded: {company_id}")
    print(f"KB path: {target_dir}")
    print("")
    print("Allowed domains:")
    if isinstance(allowed_domains, list) and allowed_domains:
        for domain in allowed_domains:
            print(f"- {domain}")
    else:
        print("- none")
    print("")
    print("Explicit embed:")
    print(explicit_embed)
    print("")
    print("Autodetect embed:")
    print(autodetect_embed)
    print("")
    print("Next checks:")
    print(f"python3 backend/scripts/validate_kb.py {target_dir}")
    print(f"python3 backend/scripts/simulate_kb.py {target_dir}")
    print("docker compose restart backend")
    return target_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Опубликовать KB клиента и получить embed-код.")
    parser.add_argument("source_dir", type=Path, help="Проверенная KB или draft, например new/kb_drafts/client_id")
    parser.add_argument(
        "--clients-dir",
        type=Path,
        default=DEFAULT_CLIENTS_DIR,
        help="Куда публиковать KB. По умолчанию backend/data/clients",
    )
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help="Публичный URL backend API")
    parser.add_argument("--force", action="store_true", help="Перезаписать существующего клиента")
    parser.add_argument("--dry-run", action="store_true", help="Показать preview без публикации файлов")
    args = parser.parse_args()

    try:
        onboard_client(
            args.source_dir,
            clients_dir=args.clients_dir,
            api_base=args.api_base,
            force=args.force,
            dry_run=args.dry_run,
        )
    except Exception as error:
        print(f"Client onboarding failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
