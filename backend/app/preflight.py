"""preflight: одна read-only проверка «всё ли на месте» — до правки и после деплоя.

Ничего не пишет, не создаёт лидов и не шлёт сообщений клиентам; значения секретов не
показывает (только булевы признаки). Наружу ходит только в Telegram (getMe/getChatMember —
оба read-only), и это отключается параметром network=0.

Структура ответа: у каждой проверки status (ok | degraded | error | skip) и detail; общий
статус — худший из всех, включая вложенные (клиенты).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI

from .health_checks import collect_health_checks
from .models import SessionStatus
from .policy.extractors import extract_phone
from .utils.jsonl import read_jsonl

logger = logging.getLogger(__name__)

# момент импорта модуля ≈ старт процесса: по нему видно, что сервер недавно перезапускался.
PROCESS_STARTED_AT = time.time()

DEFAULT_OPERATOR_TOKEN = "demo-operator-token"
MIN_OPERATOR_TOKEN_LENGTH = 12
TELEGRAM_CHECK_TIMEOUT_SECONDS = 15.0
TAIL_BYTES = 16 * 1024
ANALYTICS_LARGE_MB = 30.0
SNAPSHOT_STALE_SECONDS = 3 * 3600
FAILURES_WINDOW_SECONDS = 24 * 3600
LOW_DISK_DEGRADED_PCT = 15.0
LOW_DISK_ERROR_PCT = 5.0

# образцы для проверки распознавания телефона (та же функция, что в реальном сборе лидов)
PHONE_SAMPLES = (
    ("мой номер 8 (926) 123-45-67", "+79261234567"),
    ("926 123-45-67", "+79261234567"),
    ("+7 926 123 45 67", "+79261234567"),
)

_SEVERITY = {"ok": 0, "skip": 0, "degraded": 1, "unavailable": 1, "error": 2}


def _item(status: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "detail": detail, **extra}


def _worst(statuses: list[str]) -> str:
    worst = "ok"
    for status in statuses:
        if _SEVERITY.get(status, 1) > _SEVERITY.get(worst, 0):
            worst = "error" if _SEVERITY.get(status, 1) >= 2 else "degraded"
    return worst


def _mb(size_bytes: int) -> float:
    return round(size_bytes / 1024 / 1024, 2)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _age_text(moment: datetime) -> str:
    seconds = max(int((datetime.now(timezone.utc) - moment).total_seconds()), 0)
    if seconds < 90:
        return f"{seconds} с назад"
    if seconds < 90 * 60:
        return f"{seconds // 60} мин назад"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} ч назад"
    return f"{seconds // 86400} дн назад"


def _tail_last_record(path: Path) -> dict[str, Any] | None:
    """последняя валидная json-строка файла: читает только хвост, а не весь файл."""

    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(size - TAIL_BYTES, 0))
            chunk = handle.read()
    except OSError:
        return None
    for line in reversed(chunk.decode("utf-8", errors="ignore").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _writable(path: Path) -> bool:
    """файл (или ближайшая существующая папка над ним) доступен на запись."""

    target = path
    while not target.exists() and target != target.parent:
        target = target.parent
    return os.access(target, os.W_OK)


def check_app(settings: Any) -> dict[str, Any]:
    uptime = int(time.time() - PROCESS_STARTED_AT)
    minutes, seconds = divmod(uptime, 60)
    hours, minutes = divmod(minutes, 60)
    human = f"{hours} ч {minutes} мин" if hours else f"{minutes} мин {seconds} с"
    return _item(
        "ok",
        f"работает {human}",
        app_env=settings.app_env,
        dev_mode=bool(settings.dev_mode),
        uptime_seconds=uptime,
        started_at=_iso(datetime.fromtimestamp(PROCESS_STARTED_AT, tz=timezone.utc)),
    )


def check_operator_token(settings: Any) -> dict[str, Any]:
    token = settings.operator_token or ""
    is_default = token == DEFAULT_OPERATOR_TOKEN
    too_short = len(token) < MIN_OPERATOR_TOKEN_LENGTH
    if is_default:
        status = "degraded" if settings.dev_mode else "error"
        detail = f"токен оператора — значение по умолчанию ({DEFAULT_OPERATOR_TOKEN})"
    elif too_short:
        status = "degraded"
        detail = f"токен оператора короче {MIN_OPERATOR_TOKEN_LENGTH} символов"
    else:
        status = "ok"
        detail = "токен оператора задан (значение не показывается)"
    return _item(status, detail, is_default=is_default, dev_mode=bool(settings.dev_mode))


def _client_summary(resolver: Any, company_id: str) -> dict[str, Any]:
    try:
        kb = resolver.get(company_id, fallback=False)
    except Exception as error:  # битая папка клиента не должна ронять весь отчёт
        return {"company_id": company_id, "status": "error", "detail": f"не загрузилась: {type(error).__name__}"}

    clinic_info = kb.config_payload.get("clinic_info") if isinstance(kb.config_payload, dict) else None
    topics = clinic_info.get("sensitive_topics") if isinstance(clinic_info, dict) else None
    company = kb.company
    problems: list[str] = []
    status = "ok"
    if not kb.services:
        status, problems = "error", problems + ["нет услуг"]
    if not kb.prices:
        status = _worst([status, "degraded"])
        problems.append("нет цен")
    if not str(company.phone or "").strip():
        status = _worst([status, "degraded"])
        problems.append("нет телефона")
    return {
        "company_id": company_id,
        "status": status,
        "detail": ", ".join(problems) if problems else "данные на месте",
        "services": len(kb.services),
        "prices": len(kb.prices),
        "quick_faq": len(kb.quick_faq),
        "article_map": len(kb.article_service_map),
        "phrasebook_keys": len(kb.phrasebook),
        "sensitive_topics": len(topics) if isinstance(topics, list) else 0,
        "domains": list(company.allowed_domains),
        "has_phone": bool(str(company.phone or "").strip()),
        "has_website": bool(company.website_url),
        "has_telegram_url": bool(company.telegram_url),
    }


def check_clients(app: FastAPI) -> dict[str, Any]:
    resolver = app.state.knowledge_base_resolver
    settings = app.state.settings
    clients_dir = resolver.clients_data_dir
    company_ids = sorted(item.name for item in clients_dir.iterdir() if item.is_dir()) if clients_dir.exists() else []
    clients = [_client_summary(resolver, company_id) for company_id in company_ids]

    default_id = settings.default_company_id
    default_ok = resolver.client_exists(default_id)
    default_company = _item(
        "ok" if default_ok else "degraded",
        f"default_company_id={default_id}" + ("" if default_ok else ": такого клиента нет"),
        company_id=default_id,
        exists=default_ok,
    )
    status = _worst([client["status"] for client in clients] + [default_company["status"]])
    return _item(status, f"клиентов: {len(clients)}", clients=clients, default_company=default_company)


def check_domains(app: FastAPI) -> dict[str, Any]:
    """домен должен резолвиться ровно в одного клиента (та же логика, что в domain-check)."""

    domain_index = app.state.knowledge_base_resolver.build_domain_index()
    entries: list[dict[str, Any]] = []
    for domain, company_ids in sorted(domain_index.items()):
        if domain == "localhost":
            continue
        if len(company_ids) > 1:
            entries.append({"domain": domain, "status": "degraded", "companies": list(company_ids)})
        else:
            entries.append({"domain": domain, "status": "ok", "companies": list(company_ids)})
    duplicated = [entry["domain"] for entry in entries if entry["status"] != "ok"]
    if duplicated:
        return _item("degraded", f"домен привязан к нескольким клиентам: {', '.join(duplicated)}", domains=entries)
    return _item("ok", f"доменов: {len(entries)}, каждый указывает на одного клиента", domains=entries)


def _phone_selftest() -> list[str]:
    failures: list[str] = []
    for text, expected in PHONE_SAMPLES:
        try:
            actual = extract_phone(text)
        except Exception as error:
            actual = f"{type(error).__name__}"
        if actual != expected:
            failures.append(text)
    return failures


def check_leads(settings: Any) -> dict[str, Any]:
    path = Path(settings.leads_file)
    writable = _writable(path)
    size = path.stat().st_size if path.exists() else 0
    last = _tail_last_record(path) if path.exists() else None
    last_at = _parse_ts(last.get("timestamp")) if last else None
    phone_failures = _phone_selftest()

    problems: list[str] = []
    status = "ok"
    if not writable:
        status = "error"
        problems.append("файл лидов недоступен на запись")
    if phone_failures:
        status = "error"
        problems.append("распознавание телефона не проходит образцы")
    if problems:
        detail = "; ".join(problems)
    elif last_at is not None:
        detail = f"лиды пишутся; последний {_age_text(last_at)}"
    else:
        detail = "лиды пишутся; лидов пока нет"
    return _item(
        status,
        detail,
        writable=writable,
        size_mb=_mb(size),
        last_lead_at=_iso(last_at) if last_at else None,
        last_lead_company=(last or {}).get("company_id") if last else None,
        phone_selftest_failed=len(phone_failures),
    )


def check_analytics(settings: Any) -> dict[str, Any]:
    path = Path(settings.analytics_file)
    writable = _writable(path)
    size = path.stat().st_size if path.exists() else 0
    last = _tail_last_record(path) if path.exists() else None
    last_at = _parse_ts(last.get("timestamp")) if last else None
    size_mb = _mb(size)

    if not writable:
        status, detail = "error", "файл аналитики недоступен на запись"
    elif size > ANALYTICS_LARGE_MB * 1024 * 1024:
        status = "degraded"
        detail = f"файл аналитики {size_mb} МБ: дашборд читает его целиком, пора думать о БД"
    elif last_at is not None:
        status, detail = "ok", f"аналитика пишется; последнее событие {_age_text(last_at)}"
    else:
        status, detail = "ok", "аналитика пишется; событий пока нет"
    return _item(
        status,
        detail,
        writable=writable,
        size_mb=size_mb,
        last_event_at=_iso(last_at) if last_at else None,
    )


def check_storage(settings: Any) -> dict[str, Any]:
    logs_dir = Path(settings.logs_dir)
    probe = logs_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    usage = shutil.disk_usage(probe)
    free_pct = round(usage.free / usage.total * 100, 1) if usage.total else 0.0
    files = {}
    for name in (
        "leads_file",
        "leads_archive_file",
        "analytics_file",
        "conversations_archive_file",
        "delivery_outbox_file",
        "telegram_bridge_failures_file",
    ):
        path = getattr(settings, name, None)
        if path is not None and Path(path).exists():
            files[Path(path).name] = _mb(Path(path).stat().st_size)

    if free_pct < LOW_DISK_ERROR_PCT:
        status = "error"
    elif free_pct < LOW_DISK_DEGRADED_PCT:
        status = "degraded"
    else:
        status = "ok"
    return _item(
        status,
        f"свободно {free_pct}% диска ({round(usage.free / 1024 ** 3, 1)} ГБ)",
        free_pct=free_pct,
        free_gb=round(usage.free / 1024 ** 3, 1),
        files_mb=files,
    )


async def check_sessions(app: FastAPI) -> dict[str, Any]:
    settings = app.state.settings
    sessions = await app.state.session_store.list_all()
    by_status = {status.value: 0 for status in SessionStatus}
    for session in sessions:
        by_status[session.status.value] = by_status.get(session.status.value, 0) + 1

    snapshot_path = Path(settings.session_snapshot_file) if settings.session_snapshot_file else None
    snapshot: dict[str, Any] = {"configured": snapshot_path is not None}
    status = "ok"
    detail = f"сессий в памяти: {len(sessions)}"
    if snapshot_path is not None:
        if snapshot_path.exists():
            modified = datetime.fromtimestamp(snapshot_path.stat().st_mtime, tz=timezone.utc)
            age = (datetime.now(timezone.utc) - modified).total_seconds()
            snapshot.update({"exists": True, "updated_at": _iso(modified), "size_mb": _mb(snapshot_path.stat().st_size)})
            if settings.session_eviction_enabled and age > SNAPSHOT_STALE_SECONDS:
                status = "degraded"
                detail += f"; снапшот сессий давно не обновлялся ({_age_text(modified)})"
        else:
            snapshot["exists"] = False
    return _item(status, detail, total=len(sessions), by_status=by_status, snapshot=snapshot)


def check_logging() -> dict[str, Any]:
    """видны ли INFO-логи приложения (без logging.basicConfig они молча теряются)."""

    root = logging.getLogger()
    effective = logging.getLogger("app").getEffectiveLevel()
    visible = bool(root.handlers) and effective <= logging.INFO
    return _item(
        "ok" if visible else "degraded",
        "INFO-логи приложения выводятся" if visible else "INFO-логи приложения не выводятся (не настроено логирование)",
        effective_level=logging.getLevelName(effective),
        root_handlers=len(root.handlers),
    )


def _recent_telegram_failures(settings: Any) -> dict[str, Any]:
    path = getattr(settings, "telegram_bridge_failures_file", None)
    if path is None or not Path(path).exists():
        return {"last_24h": 0, "last_at": None}
    cutoff = time.time() - FAILURES_WINDOW_SECONDS
    recent = 0
    last_at: datetime | None = None
    for record in read_jsonl(Path(path)):
        moment = _parse_ts(record.get("timestamp"))
        if moment is None:
            continue
        if last_at is None or moment > last_at:
            last_at = moment
        if moment.timestamp() >= cutoff:
            recent += 1
    return {"last_24h": recent, "last_at": _iso(last_at) if last_at else None}


async def check_telegram(app: FastAPI, *, include_network: bool) -> dict[str, Any]:
    settings = app.state.settings
    bridge = getattr(app.state, "telegram_bridge_service", None)
    config = {
        "bridge_enabled_setting": bool(settings.telegram_bridge_enabled),
        "bot_token_set": bool(settings.telegram_bot_token),
        "operators_group_set": bool(settings.telegram_operators_group_id),
        "clients_topic_set": bool(settings.telegram_clients_topic_id),
        "dm_enabled": bool(settings.telegram_dm_enabled),
        "proxy_set": bool(settings.telegram_proxy_url),
    }
    failures = _recent_telegram_failures(settings)
    extra = {"config": config, "failures": failures}

    if bridge is None or not bridge.enabled:
        status = "skip" if settings.dev_mode else "degraded"
        return _item(status, "Telegram отключён: нет токена бота или группы операторов", **extra)

    problems: list[str] = []
    status = "ok"
    if not config["clients_topic_set"]:
        status = "degraded"
        problems.append("не задана тема для карточек лидов (карточки лидов не отправляются)")
    if failures["last_24h"]:
        status = _worst([status, "degraded"])
        problems.append(f"ошибок отправки за сутки: {failures['last_24h']}")

    if not include_network:
        return _item(status, "; ".join(problems) or "настроен (сеть не проверялась)", network_checked=False, **extra)

    try:
        live = await asyncio.wait_for(bridge.health_check(), timeout=TELEGRAM_CHECK_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return _item("error", f"Telegram не ответил за {int(TELEGRAM_CHECK_TIMEOUT_SECONDS)} с", network_checked=True, **extra)
    except Exception as error:
        return _item("error", f"проверка Telegram упала: {type(error).__name__}", network_checked=True, **extra)

    bot_token = live.get("bot_token") or {}
    group = live.get("operators_group") or {}
    status = _worst([status, str(bot_token.get("status", "ok")), str(group.get("status", "ok"))])
    detail_parts = [f"бот: {bot_token.get('detail')}", f"группа: {group.get('detail')}", *problems]
    return _item(status, "; ".join(detail_parts), network_checked=True, bot_token=bot_token, operators_group=group, **extra)


def _safe(name: str, check: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        return check()
    except Exception as error:  # одна упавшая проверка не должна ломать весь отчёт
        logger.warning("preflight check_failed name=%s error=%s", name, type(error).__name__)
        return _item("error", f"проверка упала: {type(error).__name__}")


def _with_detail(name: str, item: dict[str, Any]) -> dict[str, Any]:
    """у проверок из /health не у всех есть detail (например, delivery) — дополняем для единого вида."""

    if "detail" in item:
        return item
    if name == "delivery":
        detail = (
            f"в очереди: {item.get('pending_events', 0)}, "
            f"мёртвых за последнее время: {item.get('dead_events', 0)}"
        )
    else:
        detail = str(item.get("status", ""))
    return {**item, "detail": detail}


def _collect_sync(app: FastAPI) -> dict[str, dict[str, Any]]:
    settings = app.state.settings
    checks: dict[str, dict[str, Any]] = {}
    checks["app"] = _safe("app", lambda: check_app(settings))
    checks["operator_token"] = _safe("operator_token", lambda: check_operator_token(settings))
    try:
        for name, item in collect_health_checks(app).items():
            checks[name] = _with_detail(name, item)
    except Exception as error:
        logger.warning("preflight check_failed name=health error=%s", type(error).__name__)
        checks["health"] = _item("error", f"проверка упала: {type(error).__name__}")
    checks["clients"] = _safe("clients", lambda: check_clients(app))
    checks["domains"] = _safe("domains", lambda: check_domains(app))
    checks["leads"] = _safe("leads", lambda: check_leads(settings))
    checks["analytics"] = _safe("analytics", lambda: check_analytics(settings))
    checks["storage"] = _safe("storage", lambda: check_storage(settings))
    checks["logging"] = _safe("logging", check_logging)
    return checks


def _flatten_statuses(checks: dict[str, dict[str, Any]]) -> list[str]:
    statuses: list[str] = []
    for item in checks.values():
        statuses.append(str(item.get("status", "ok")))
        for client in item.get("clients", []) or []:
            statuses.append(str(client.get("status", "ok")))
    return statuses


async def run_preflight(app: FastAPI, *, include_network: bool = True) -> dict[str, Any]:
    checks = await asyncio.to_thread(_collect_sync, app)
    try:
        checks["sessions"] = await check_sessions(app)
    except Exception as error:
        logger.warning("preflight check_failed name=sessions error=%s", type(error).__name__)
        checks["sessions"] = _item("error", f"проверка упала: {type(error).__name__}")
    try:
        checks["telegram"] = await check_telegram(app, include_network=include_network)
    except Exception as error:
        logger.warning("preflight check_failed name=telegram error=%s", type(error).__name__)
        checks["telegram"] = _item("error", f"проверка упала: {type(error).__name__}")

    statuses = _flatten_statuses(checks)
    summary = {name: statuses.count(name) for name in ("ok", "degraded", "error", "skip")}
    return {
        "status": _worst(statuses),
        "generated_at": _iso(datetime.now(timezone.utc)),
        "network_checked": include_network,
        "summary": summary,
        "checks": checks,
    }
