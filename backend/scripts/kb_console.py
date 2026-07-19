"""Интерактивная консоль для правки client KB.

Личный инструмент: вводишь данные — раскладывается по нужным файлам
(services.json / prices.json / config.yaml), без ручного лазания по yaml.

Валидация сейчас минимальная (не пусто / похоже на число). Это осознанный
выбор для v1 "для себя" — collect() / validate() / write() внутри каждого
флоу разделены нарочно, чтобы позже подключить строгую проверку (дубли id,
конфликты blocked_values и т.д.), не переписывая сами флоу.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

import questionary
import yaml
from questionary import Style
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table


REPO_ROOT = Path(__file__).resolve().parents[2]
CLIENTS_DIR = REPO_ROOT / "backend" / "data" / "clients"
VALIDATE_KB_SCRIPT = Path(__file__).resolve().parent / "validate_kb.py"

console = Console()

QS_STYLE = Style(
    [
        ("qmark", "fg:#1F7A5C bold"),
        ("question", "bold"),
        ("answer", "fg:#1F7A5C bold"),
        ("pointer", "fg:#1F7A5C bold"),
        ("highlighted", "fg:#1F7A5C bold"),
        ("selected", "fg:#1F7A5C"),
    ]
)

FACT_LABELS = {
    "oms": "Приём по ОМС",
    "ambulance_brings": "Скорая привозит к вам",
    "sells_products": "Продаёте товары/косметику",
    "discloses_doctor_schedule": "Называете расписание врачей",
}

DEFAULT_PHRASEBOOK_KEYS = [
    "company_noun", "service_word", "operator_label", "booking_word",
    "price_disclaimer", "unknown_service", "off_topic", "contact_prompt",
    "booking_contact_prompt", "handoff_message", "operator_soft_offer",
    "regulated_soft_offer", "clarify", "empty_message", "empty_message_letters",
    "rate_limit", "human_active_wait", "contact_cancelled", "booking_cancelled",
    "general_cancelled", "lead_success", "booking_success", "clinic_location",
    "clinic_location_deferred", "doctors_from_data", "doctors_deferred",
    "doctor_schedule_deferred", "fact_oms_no", "fact_oms_yes",
    "fact_ambulance_no", "fact_ambulance_yes", "fact_products_no",
    "fact_products_yes", "clinic_fact_deferred", "equipment_deferred",
    "medical_referral", "sensitive_escalate", "sensitive_decline",
    "efficacy_claim_deferred", "lead_followup",
]

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _slugify(value: str) -> str:
    import re

    value = value.lower()
    transliterated = "".join(TRANSLIT.get(char, char) for char in value)
    transliterated = re.sub(r"[^a-z0-9]+", "_", transliterated)
    transliterated = re.sub(r"_+", "_", transliterated).strip("_")
    return transliterated or "service"


def _stable_id(company_id: str, category: str, name: str) -> str:
    digest = hashlib.sha1(f"{company_id}|{category}|{name}".encode("utf-8")).hexdigest()[:8]
    slug = _slugify(name)[:56].strip("_")
    return f"{slug}_{digest}"


# --- валидаторы (минимальные сейчас — задел на строгие позже) -------------

def not_empty(text: str) -> "bool | str":
    return True if text.strip() else "Не может быть пустым."


def optional_number(text: str) -> "bool | str":
    text = text.strip()
    if not text:
        return True
    cleaned = text.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        float(cleaned)
    except ValueError:
        return "Похоже, это не число."
    return True


# company_id — единственное место, где валидация СТРОГАЯ уже сейчас, не "задел
# на потом": это то же самое правило, которым secure path resolver в runtime
# защищается от directory traversal (см. CLIENT_ID_PATTERN в app/knowledge.py).
# Кривой company_id — не "неаккуратные данные", а потенциально сломанный путь.
CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def valid_new_company_id(text: str) -> "bool | str":
    text = text.strip()
    if not text:
        return "Не может быть пустым."
    if not CLIENT_ID_PATTERN.match(text):
        return "Только латиница, цифры, - и _ (это имя папки — без пробелов и кириллицы)."
    if (CLIENTS_DIR / text).exists():
        return "Такой клиент уже существует."
    return True


# --- IO helpers -------------------------------------------------------------

def _backup(path: Path) -> None:
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, backup_path)


def _load_json(path: Path) -> Any:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def _save_json(path: Path, data: Any) -> None:
    _backup(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_config(company_dir: Path) -> dict:
    path = company_dir / "config.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _save_config(company_dir: Path, config: dict) -> None:
    path = company_dir / "config.yaml"
    _backup(path)
    path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _run_validate_kb(company_dir: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(VALIDATE_KB_SCRIPT), str(company_dir)],
        capture_output=True,
        text=True,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        console.print(
            Panel(output or "Замечаний нет.", title="validate_kb.py", border_style="green")
        )
    else:
        console.print(
            Panel(output, title="validate_kb.py — есть замечания", border_style="red")
        )


# --- выбор клиента ----------------------------------------------------------

NEW_CLIENT_SENTINEL = "__new_client__"


def choose_company() -> Optional[Path]:
    companies = sorted(
        p.name for p in CLIENTS_DIR.iterdir() if p.is_dir() and (p / "company.yaml").exists()
    )
    choices = [questionary.Choice(title="➕ Создать нового клиента", value=NEW_CLIENT_SENTINEL)]
    choices += companies
    default = "rosh_import_demo" if "rosh_import_demo" in companies else NEW_CLIENT_SENTINEL
    choice = questionary.select(
        "С каким клиентом работаем?", choices=choices, default=default, style=QS_STYLE
    ).ask()
    if choice is None:
        return None
    if choice == NEW_CLIENT_SENTINEL:
        return flow_create_client()
    return CLIENTS_DIR / choice


def flow_create_client() -> Optional[Path]:
    console.print(Panel("Новый клиент", style="cyan"))
    company_id = questionary.text(
        "company_id (латиница/цифры/-/_ — это же имя папки):",
        validate=valid_new_company_id,
        style=QS_STYLE,
    ).ask()
    if company_id is None:
        return None
    company_id = company_id.strip()

    company_name = questionary.text("Название компании:", validate=not_empty, style=QS_STYLE).ask()
    city = questionary.text("Город:", validate=not_empty, style=QS_STYLE).ask()
    phone = questionary.text("Телефон:", validate=not_empty, style=QS_STYLE).ask()
    working_hours = questionary.text(
        "Режим работы (например 'Пн-Пт 9:00-20:00'):", validate=not_empty, style=QS_STYLE
    ).ask()
    address = questionary.text("Адрес (можно пусто — 'уточняется'):", style=QS_STYLE).ask()
    website_url = questionary.text("Сайт (можно пусто):", style=QS_STYLE).ask()
    telegram_url = questionary.text("Telegram (можно пусто):", style=QS_STYLE).ask()
    allowed_domains_raw = questionary.text(
        "Разрешённые домены через запятую:", default="localhost", validate=not_empty, style=QS_STYLE
    ).ask()
    safety_disclaimer = questionary.text(
        "Дисклеймер безопасности:",
        default="Я не врач и не ставлю диагнозы. По медицинским вопросам вас сориентирует специалист.",
        validate=not_empty,
        style=QS_STYLE,
    ).ask()

    collected = [
        company_name, city, phone, working_hours, address,
        website_url, telegram_url, allowed_domains_raw, safety_disclaimer,
    ]
    if any(v is None for v in collected):
        console.print("[yellow]Отменено.[/yellow]")
        return None

    table = Table(box=box.ROUNDED, show_header=False)
    table.add_row("company_id", company_id)
    table.add_row("Название", company_name)
    table.add_row("Город", city)
    table.add_row("Телефон", phone)
    table.add_row("Режим работы", working_hours)
    table.add_row("Домены", allowed_domains_raw)
    console.print(Panel(table, title="Сводка перед созданием", border_style="green"))

    if not questionary.confirm("Создать клиента?", default=True, style=QS_STYLE).ask():
        console.print("[yellow]Отменено.[/yellow]")
        return None

    company_dir = CLIENTS_DIR / company_id
    company_dir.mkdir(parents=True)

    company_yaml = {
        "company_id": company_id,
        "company_name": company_name,
        "city": city,
        "working_hours": working_hours,
        "phone": phone,
        "address": address.strip() if address and address.strip() else "уточняется",
        "website_url": website_url.strip() if website_url else "",
        "telegram_url": telegram_url.strip() if telegram_url else "",
        "allowed_domains": [d.strip() for d in allowed_domains_raw.split(",") if d.strip()],
        "allowed_topics": ["услуги центра", "цены", "запись", "адрес и режим работы"],
        "operator_triggers": [
            "оператор", "менеджер", "живой человек", "администратор", "специалист", "человек",
        ],
        "forbidden_claims": [
            "диагнозы", "назначение препаратов", "гарантии результата",
            "медицинские рекомендации", "услуги не из базы",
        ],
        "safety_disclaimer": safety_disclaimer,
    }
    (company_dir / "company.yaml").write_text(
        yaml.safe_dump(company_yaml, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    (company_dir / "services.json").write_text("[]\n", encoding="utf-8")
    (company_dir / "prices.json").write_text("[]\n", encoding="utf-8")
    (company_dir / "faq.md").write_text(
        "# FAQ\n\n"
        "## Услуги\n\nTODO: заполнить.\n\n"
        "## Цены\n\nTODO: заполнить.\n\n"
        "## Запись\n\nTODO: заполнить.\n\n"
        "## Медицинские вопросы\n\n"
        "Виджет не ставит диагнозы, не назначает лечение и не даёт индивидуальные "
        "медицинские рекомендации. Такие вопросы передаются специалисту.\n",
        encoding="utf-8",
    )

    console.print(f"[green]Клиент {company_id} создан.[/green]")
    console.print(
        "[cyan]validate_kb.py потребует хотя бы одну услугу — добавим первую сразу.[/cyan]\n"
    )
    flow_service(company_dir)

    return company_dir


# --- флоу: услуга + цена -----------------------------------------------------

def flow_service(company_dir: Path) -> bool:
    services = _load_json(company_dir / "services.json")
    prices = _load_json(company_dir / "prices.json")

    console.print(Panel("Новая услуга или обновление существующей", style="cyan"))
    name = questionary.text("Название услуги:", validate=not_empty, style=QS_STYLE).ask()
    if name is None:
        return False

    existing = next(
        (s for s in services if str(s.get("name", "")).strip().lower() == name.strip().lower()),
        None,
    )
    if existing:
        console.print(
            f"[yellow]Нашёл существующую услугу «{name}» (id={existing['id']}) — обновляю её.[/yellow]"
        )

    category = questionary.text(
        "Категория:",
        default=(existing.get("category") or name) if existing else name,
        validate=not_empty,
        style=QS_STYLE,
    ).ask()
    short_description = questionary.text(
        "Короткое описание:",
        default=(existing.get("short_description") if existing else f"Направление «{name}»."),
        style=QS_STYLE,
    ).ask()
    # Цену НЕ предзаполняем старым значением в поле ввода: если юзер печатает
    # новую цену не глядя (не очистив предзаполненное), цифры молча
    # склеиваются со старыми (1500 + напечатанные 2000 = 15002000) — реальный
    # баг, пойманный на тестировании. Вместо этого старое значение — подсказка
    # в тексте вопроса, пустой ввод = "оставить как было".
    price_from_hint = (
        f" (сейчас: {existing['price_from']}, Enter — не менять)"
        if existing and existing.get("price_from") is not None
        else ""
    )
    price_from_raw = questionary.text(
        f"Цена от (число, можно пусто){price_from_hint}:",
        default="",
        validate=optional_number,
        style=QS_STYLE,
    ).ask()
    price_to_hint = (
        f" (сейчас: {existing['price_to']}, Enter — не менять)"
        if existing and existing.get("price_to") is not None
        else ""
    )
    price_to_raw = questionary.text(
        f"Цена до (если диапазон; пусто — не менять/совпадает с 'от'){price_to_hint}:",
        default="",
        validate=optional_number,
        style=QS_STYLE,
    ).ask()
    duration = questionary.text(
        "Длительность (можно пусто):",
        default=(existing.get("duration") or "") if existing else "",
        style=QS_STYLE,
    ).ask()
    synonyms_raw = questionary.text(
        "Синонимы через запятую (можно пусто):",
        default=", ".join(existing.get("synonyms", [])) if existing else "",
        style=QS_STYLE,
    ).ask()
    requires_specialist = questionary.confirm(
        "Требует специалиста (влияет на дисклеймеры)?",
        default=bool(existing.get("requires_specialist", True)) if existing else True,
        style=QS_STYLE,
    ).ask()

    collected = [category, short_description, price_from_raw, price_to_raw, duration, synonyms_raw]
    if any(v is None for v in collected) or requires_specialist is None:
        console.print("[yellow]Отменено.[/yellow]")
        return False

    if price_from_raw.strip():
        price_from = int(float(price_from_raw.replace(",", ".")))
    else:
        price_from = existing.get("price_from") if existing else None

    if price_to_raw.strip():
        price_to = int(float(price_to_raw.replace(",", ".")))
    elif existing:
        price_to = existing.get("price_to")
    else:
        price_to = price_from

    if price_from is not None and price_to is not None and price_from != price_to:
        price_range_text = f"от {price_from:,} до {price_to:,} ₽".replace(",", " ")
    elif price_from is not None:
        price_range_text = f"{price_from:,} ₽".replace(",", " ")
    else:
        price_range_text = None

    synonyms = [s.strip() for s in synonyms_raw.split(",") if s.strip()]
    service_id = existing["id"] if existing else _stable_id(company_dir.name, category, name)

    service_entry = {
        "id": service_id,
        "name": name,
        "category": category,
        "synonyms": synonyms,
        "short_description": short_description,
        "price_from": price_from,
        "price_to": price_to,
        "price_range_text": price_range_text,
        "duration": duration or None,
        "requires_specialist": requires_specialist,
        "variants": existing.get("variants", []) if existing else [],
    }

    table = Table(box=box.ROUNDED, show_header=False)
    table.add_row("ID", service_id)
    table.add_row("Название", name)
    table.add_row("Категория", category)
    table.add_row("Цена", price_range_text or "не указана")
    table.add_row("Синонимы", ", ".join(synonyms) or "—")
    console.print(Panel(table, title="Сводка перед записью", border_style="green"))

    if not questionary.confirm("Записать?", default=True, style=QS_STYLE).ask():
        console.print("[yellow]Отменено.[/yellow]")
        return False

    if existing:
        services = [service_entry if s["id"] == service_id else s for s in services]
    else:
        services.append(service_entry)
    _save_json(company_dir / "services.json", services)

    price_text = price_range_text or "цена уточняется"
    price_entry = {
        "service_id": service_id,
        "price_text": price_text,
        "comment": next((p.get("comment", "") for p in prices if p.get("service_id") == service_id), ""),
    }
    if any(p.get("service_id") == service_id for p in prices):
        prices = [price_entry if p.get("service_id") == service_id else p for p in prices]
    else:
        prices.append(price_entry)
    _save_json(company_dir / "prices.json", prices)

    console.print("[green]Готово: услуга и цена записаны.[/green]")
    return True


# --- флоу: fact_guard ---------------------------------------------------------

def flow_fact_guard(company_dir: Path) -> bool:
    config = _load_config(company_dir)
    fact_guards = config.get("fact_guards") or []

    console.print(Panel("Блокировка препарата/бренда/факта (fact_guard)", style="cyan"))
    topic = questionary.text("Тема (например 'ботулинотерапия'):", validate=not_empty, style=QS_STYLE).ask()
    if topic is None:
        return False
    known_raw = questionary.text("Разрешённые значения через запятую (можно пусто):", style=QS_STYLE).ask()
    blocked_raw = questionary.text(
        "Запрещённые значения через запятую:", validate=not_empty, style=QS_STYLE
    ).ask()
    message = questionary.text(
        "Кастомное сообщение юзеру (можно пусто — возьмётся дефолтное):", style=QS_STYLE
    ).ask()
    if any(v is None for v in [known_raw, blocked_raw, message]):
        console.print("[yellow]Отменено.[/yellow]")
        return False

    entry: dict[str, Any] = {
        "topic": topic,
        "service_id": None,
        "known_values": [v.strip() for v in known_raw.split(",") if v.strip()],
        "blocked_values": [v.strip() for v in blocked_raw.split(",") if v.strip()],
    }
    if message.strip():
        entry["message_to_user"] = message.strip()

    table = Table(box=box.ROUNDED, show_header=False)
    table.add_row("Тема", topic)
    table.add_row("Разрешено", ", ".join(entry["known_values"]) or "—")
    table.add_row("Запрещено", ", ".join(entry["blocked_values"]))
    console.print(Panel(table, title="Сводка перед записью", border_style="green"))

    if not questionary.confirm("Записать?", default=True, style=QS_STYLE).ask():
        console.print("[yellow]Отменено.[/yellow]")
        return False

    fact_guards.append(entry)
    config["fact_guards"] = fact_guards
    _save_config(company_dir, config)
    console.print("[green]Готово: fact_guard добавлен.[/green]")
    return True


# --- флоу: факт о клинике -----------------------------------------------------

def flow_clinic_fact(company_dir: Path) -> bool:
    config = _load_config(company_dir)
    clinic_info = config.get("clinic_info") or {}
    facts = clinic_info.get("facts") or {}

    console.print(Panel("Факты о клинике", style="cyan"))
    table = Table(box=box.ROUNDED, show_header=False)
    for key, label in FACT_LABELS.items():
        table.add_row(label, "да" if facts.get(key) else "нет")
    console.print(table)

    key = questionary.select(
        "Какой факт поменять?",
        choices=[questionary.Choice(title=label, value=k) for k, label in FACT_LABELS.items()],
        style=QS_STYLE,
    ).ask()
    if key is None:
        return False
    new_value = questionary.confirm(
        f"{FACT_LABELS[key]} — включить?", default=bool(facts.get(key)), style=QS_STYLE
    ).ask()
    if new_value is None:
        return False

    if not questionary.confirm("Записать?", default=True, style=QS_STYLE).ask():
        console.print("[yellow]Отменено.[/yellow]")
        return False

    facts[key] = new_value
    clinic_info["facts"] = facts
    config["clinic_info"] = clinic_info
    _save_config(company_dir, config)
    console.print("[green]Готово: факт обновлён.[/green]")
    return True


# --- флоу: деликатная тема ----------------------------------------------------

def flow_sensitive_topic(company_dir: Path) -> bool:
    config = _load_config(company_dir)
    clinic_info = config.get("clinic_info") or {}
    topics = clinic_info.get("sensitive_topics") or []

    console.print(Panel("Деликатная тема", style="cyan"))
    keywords_raw = questionary.text("Ключевые слова через запятую:", validate=not_empty, style=QS_STYLE).ask()
    if keywords_raw is None:
        return False
    handling = questionary.select(
        "Как обрабатывать?",
        choices=[
            questionary.Choice(title="escalate — мягко предложить специалиста", value="escalate"),
            questionary.Choice(title="decline — прямо сказать, что не делаем", value="decline"),
        ],
        style=QS_STYLE,
    ).ask()
    if handling is None:
        return False
    text = questionary.text("Текст ответа (можно пусто — дефолтный):", style=QS_STYLE).ask()
    offer_lead = questionary.confirm(
        "Предлагать оставить контакт?", default=(handling == "escalate"), style=QS_STYLE
    ).ask()
    if text is None or offer_lead is None:
        console.print("[yellow]Отменено.[/yellow]")
        return False

    entry: dict[str, Any] = {
        "keywords": [k.strip() for k in keywords_raw.split(",") if k.strip()],
        "handling": handling,
        "offer_lead": offer_lead,
    }
    if text.strip():
        entry["text"] = text.strip()

    table = Table(box=box.ROUNDED, show_header=False)
    table.add_row("Ключевые слова", ", ".join(entry["keywords"]))
    table.add_row("Обработка", handling)
    console.print(Panel(table, title="Сводка перед записью", border_style="green"))

    if not questionary.confirm("Записать?", default=True, style=QS_STYLE).ask():
        console.print("[yellow]Отменено.[/yellow]")
        return False

    topics.append(entry)
    clinic_info["sensitive_topics"] = topics
    config["clinic_info"] = clinic_info
    _save_config(company_dir, config)
    console.print("[green]Готово: деликатная тема добавлена.[/green]")
    return True


# --- флоу: фраза (phrasebook) -------------------------------------------------

def flow_phrasebook(company_dir: Path) -> bool:
    config = _load_config(company_dir)
    phrasebook = config.get("phrasebook") or {}

    console.print(Panel("Фраза (phrasebook)", style="cyan"))
    key = questionary.select(
        "Какую фразу поменять?", choices=DEFAULT_PHRASEBOOK_KEYS, style=QS_STYLE
    ).ask()
    if key is None:
        return False
    current = phrasebook.get(key, "(сейчас используется дефолтная фраза из кода)")
    console.print(f"Сейчас: [dim]{current}[/dim]")
    new_text = questionary.text("Новый текст:", validate=not_empty, style=QS_STYLE).ask()
    if new_text is None:
        return False

    if not questionary.confirm("Записать?", default=True, style=QS_STYLE).ask():
        console.print("[yellow]Отменено.[/yellow]")
        return False

    phrasebook[key] = new_text
    config["phrasebook"] = phrasebook
    _save_config(company_dir, config)
    console.print("[green]Готово: фраза обновлена.[/green]")
    return True


MENU_CHOICES = [
    questionary.Choice(title="Услуга + цена (добавить/обновить)", value="service"),
    questionary.Choice(title="Блокировка препарата/бренда (fact_guard)", value="fact_guard"),
    questionary.Choice(title="Факт о клинике (ОМС/скорая/товары/расписание)", value="clinic_fact"),
    questionary.Choice(title="Деликатная тема", value="sensitive_topic"),
    questionary.Choice(title="Фраза (phrasebook)", value="phrasebook"),
    questionary.Choice(title="Выход", value="exit"),
]

FLOWS: dict[str, Callable[[Path], bool]] = {
    "service": flow_service,
    "fact_guard": flow_fact_guard,
    "clinic_fact": flow_clinic_fact,
    "sensitive_topic": flow_sensitive_topic,
    "phrasebook": flow_phrasebook,
}


def _banner() -> None:
    console.print(
        Panel.fit(
            "[bold]KB Console[/bold]\n"
            "[dim]Вводишь данные — раскладывается по нужным файлам.[/dim]",
            border_style="#1F7A5C",
            box=box.ROUNDED,
        )
    )


def main() -> int:
    _banner()
    company_dir = choose_company()
    if company_dir is None:
        return 0
    console.print(f"Работаем с [bold]{company_dir.name}[/bold]\n")

    while True:
        action = questionary.select("Что делаем?", choices=MENU_CHOICES, style=QS_STYLE).ask()
        if action is None or action == "exit":
            break
        FLOWS[action](company_dir)
        console.print("")

    # Не завязываемся на "было ли изменение в этом цикле" — создание нового
    # клиента тоже пишет файлы (до входа в этот цикл), и раньше это тихо
    # пропускало проверку. Дешевле спросить всегда, чем полагаться на хрупкий
    # трекинг "changed" (поймано на тестировании).
    if questionary.confirm("Прогнать validate_kb.py перед выходом?", default=True, style=QS_STYLE).ask():
        _run_validate_kb(company_dir)

    console.print("[bold]Пока![/bold]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
