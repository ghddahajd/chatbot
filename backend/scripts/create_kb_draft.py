"""создаёт черновик клиентской KB из шаблона и исходных материалов."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = REPO_ROOT / "backend" / "data" / "client_template" / "sample_client"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "new" / "kb_drafts"


class TextExtractor(HTMLParser):
    """грубый html-to-text extractor без внешних зависимостей."""

    def __init__(self) -> None:
        super().__init__()
        self._skip_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag in {"p", "br", "li", "h1", "h2", "h3", "section", "article"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if tag in {"p", "li", "h1", "h2", "h3", "section", "article"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = " ".join(data.split())
        if text:
            self._chunks.append(text)

    def text(self) -> str:
        raw_text = " ".join(self._chunks)
        lines = [line.strip() for line in raw_text.splitlines()]
        return "\n".join(line for line in lines if line)


def _yaml_value(value: str | None) -> str:
    if value is None:
        return ""
    escaped = value.replace('"', '\\"')
    return f'"{escaped}"'


def _domain_from_url(value: str | None) -> str | None:
    if not value:
        return None
    return urlparse(value).hostname


def _read_source(source: str) -> tuple[str, str]:
    if source.startswith(("http://", "https://")):
        request = Request(source, headers={"User-Agent": "chat-widget-kb-onboarding/1.0"})
        try:
            with urlopen(request, timeout=15) as response:
                content_type = response.headers.get("content-type", "")
                body = response.read().decode("utf-8", errors="ignore")
        except URLError as error:
            return source, f"[Не удалось загрузить источник: {type(error).__name__}]"

        if "html" in content_type or "<html" in body.lower():
            extractor = TextExtractor()
            extractor.feed(body)
            return source, extractor.text()
        return source, body

    path = Path(source)
    if not path.exists() or not path.is_file():
        return source, "[Файл не найден]"

    body = path.read_text(encoding="utf-8", errors="ignore")
    if path.suffix.lower() in {".html", ".htm"}:
        extractor = TextExtractor()
        extractor.feed(body)
        return str(path), extractor.text()
    return str(path), body


def _update_company_yaml(
    path: Path,
    *,
    company_id: str,
    company_name: str,
    city: str,
    phone: str,
    website_url: str | None,
    telegram_url: str | None,
    lead_webhook_url: str | None,
) -> None:
    allowed_domains = ['  - "localhost"']
    website_domain = _domain_from_url(website_url)
    if website_domain:
        allowed_domains.append(f'  - "{website_domain}"')
        if not website_domain.startswith("www."):
            allowed_domains.append(f'  - "www.{website_domain}"')

    lines = [
        f"company_id: {company_id}",
        f"company_name: {_yaml_value(company_name)}",
        f"city: {_yaml_value(city)}",
        'working_hours: "уточняется"',
        f"phone: {_yaml_value(phone)}",
        'address: "уточняется"',
        f"website_url: {_yaml_value(website_url)}",
        f"telegram_url: {_yaml_value(telegram_url)}",
        f"lead_webhook_url: {_yaml_value(lead_webhook_url)}",
        "allowed_domains:",
        *allowed_domains,
        "allowed_topics:",
        '  - "услуги"',
        '  - "цены"',
        '  - "запись"',
        '  - "режим работы"',
        '  - "адрес"',
        "operator_triggers:",
        '  - "оператор"',
        '  - "специалист"',
        '  - "человек"',
        "forbidden_claims:",
        '  - "гарантия результата"',
        '  - "точная цена без проверки"',
        '  - "регулируемая консультация без специалиста"',
        'safety_disclaimer: "По этому вопросу лучше уточнить у специалиста."',
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_faq(path: Path, sources: list[str]) -> None:
    sections = [
        "# FAQ draft",
        "",
        "Этот файл черновой. Перед публикацией нужно вручную проверить факты, услуги, цены и формулировки.",
        "",
    ]
    for source in sources:
        label, text = _read_source(source)
        sections.extend(
            [
                f"## Source: {label}",
                "",
                text[:12000].strip() or "[Пустой источник]",
                "",
            ]
        )
    if not sources:
        sections.extend(
            [
                "## Что заполнить",
                "",
                "- правила записи;",
                "- режим работы;",
                "- частые вопросы;",
                "- ограничения и дисклеймеры;",
                "- что делать, если услуга неизвестна.",
                "",
            ]
        )
    path.write_text("\n".join(sections), encoding="utf-8")


def _write_review(path: Path, company_id: str) -> None:
    path.write_text(
        "\n".join(
            [
                f"# Review checklist: {company_id}",
                "",
                "- [ ] `company.yaml`: город, контакты, ссылки, webhook и allowed_domains проверены.",
                "- [ ] `services.json`: нет услуг, которых клиент реально не оказывает.",
                "- [ ] `prices.json`: цены предварительные и привязаны к service_id.",
                "- [ ] `faq.md`: нет обещаний результата, неподтверждённых цен и регулируемых советов.",
                "- [ ] `python3 backend/scripts/validate_kb.py <kb_dir>` проходит.",
                "- [ ] `python3 backend/scripts/simulate_kb.py <kb_dir>` прогнан.",
                "- [ ] После проверки папка вручную скопирована в `backend/data/clients/`.",
                "",
                "Не публиковать draft автоматически. Это staging-материал для ручной проверки.",
                "",
            ]
        ),
        encoding="utf-8",
    )


def create_draft(args: argparse.Namespace) -> Path:
    if not CLIENT_ID_PATTERN.match(args.company_id):
        raise ValueError("company_id может содержать только латиницу, цифры, '-' и '_'")

    target_dir = args.output_root / args.company_id
    if target_dir.exists() and not args.force:
        raise FileExistsError(f"draft уже существует: {target_dir}. Используй --force для перезаписи.")

    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.copytree(TEMPLATE_DIR, target_dir)

    _update_company_yaml(
        target_dir / "company.yaml",
        company_id=args.company_id,
        company_name=args.company_name,
        city=args.city,
        phone=args.phone,
        website_url=args.website_url,
        telegram_url=args.telegram_url,
        lead_webhook_url=args.lead_webhook_url,
    )
    _write_faq(target_dir / "faq.md", args.source)
    _write_review(target_dir / "REVIEW.md", args.company_id)
    return target_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Создать черновик KB клиента.")
    parser.add_argument("--company-id", required=True, help="ID клиента, например rosh_demo")
    parser.add_argument("--company-name", required=True, help="Название клиента")
    parser.add_argument("--city", required=True, help="Город очного приёма")
    parser.add_argument("--phone", default="", help="Телефон клиента")
    parser.add_argument("--website-url", default=None, help="Сайт клиента")
    parser.add_argument("--telegram-url", default=None, help="Telegram ссылка")
    parser.add_argument("--lead-webhook-url", default=None, help="Webhook для лидов")
    parser.add_argument("--source", action="append", default=[], help="URL или локальный файл для faq.md")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--force", action="store_true", help="Перезаписать существующий draft")
    args = parser.parse_args()

    try:
        target_dir = create_draft(args)
    except Exception as error:
        print(f"KB draft failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1

    print(f"KB draft created: {target_dir}")
    print(f"Next: python3 backend/scripts/validate_kb.py {target_dir}")
    print(f"Then: python3 backend/scripts/simulate_kb.py {target_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
