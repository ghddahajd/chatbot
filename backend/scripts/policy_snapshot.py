"""сеть безопасности, слой А: снимок решений правил и сравнение двух версий кода.

Каждое сообщение корпуса проходит тот же путь, что в чате до генерации ответа: классификация
(локальная, Mock-LLM) → analyze_message. Записывается решение: действие, причина, услуга, кнопки,
текст заготовки, отпечаток контекста для LLM. Две версии кода гоняют один и тот же корпус на
одних и тех же данных клиента, различия показываются списком.

    python3 backend/scripts/policy_snapshot.py compare                  # feat/multiclient против рабочей копии
    python3 backend/scripts/policy_snapshot.py compare --base main
    python3 backend/scripts/policy_snapshot.py compare --expect ожидания.json
    python3 backend/scripts/policy_snapshot.py coverage                 # какие исходы _analyze_message_core задеты
    python3 backend/scripts/policy_snapshot.py import-live tasks/chats_all_*.json

Детерминизм: фиксированный session_id на случай, random.choice всегда берёт первый вариант (не
зависит от порядка вызовов), время зафиксировано — «день» (12:00 МСК) и «ночь» (23:30 МСК).

Данные клиента берутся из приватного репо (rosh-client-data), оверрайды вкладки «Настройки»
отключены. Результаты и отчёты — в tasks/snapshots/ (не в git). Запускать из .venv проекта.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import copy
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time as time_module
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from zoneinfo import ZoneInfo

SCRIPT_PATH = Path(__file__).resolve()
REPO_DIR = SCRIPT_PATH.parents[2]
DEFAULT_CLIENTS_DIR = REPO_DIR / "rosh-client-data" / "clients"
DEFAULT_EVAL_DIR = REPO_DIR / "rosh-client-data" / "eval"
DEFAULT_RAG_FILE = REPO_DIR / "client-input" / "normalized" / "rosh_articles" / "crawl" / "chunks.corpus.jsonl"
DEFAULT_OUT_DIR = REPO_DIR / "tasks" / "snapshots"
DEFAULT_COMPANY = "rosh_import_demo"
DEFAULT_BASE = "feat/multiclient"
MOSCOW = ZoneInfo("Europe/Moscow")
# вторник: у РОШ 10:00–21:00 каждый день
FIXED_TIMES = {
    "day": datetime(2026, 9, 22, 12, 0, tzinfo=MOSCOW),
    "night": datetime(2026, 9, 22, 23, 30, tzinfo=MOSCOW),
}
# состояния диалога, в которых гоняются короткие сообщения (реплики-продолжения). Порядок правил
# ломается там, где одновременно выполняются два условия, — а многие условия читают сессию
# (pending_action, последняя услуга, флаги лида/оператора). В чистой сессии такие поломки не видны.
FOLLOWUP_MAX_WORDS = 8
CONTEXTS = (
    "fresh",
    "offered_operator",
    "collect_contact",
    "booking_contact",
    "service_context",
    "cosmetic_candidates",
    "lead_given",
    "operator_requested",
)
COMPARED_FIELDS = (
    "error",
    "classification",
    "action",
    "reason",
    "service_id",
    "confidence",
    "quick_actions",
    "question_type",
    "message_to_user",
    "context_hash",
    "llm_input_hash",
)
CYRILLIC = re.compile(r"[А-Яа-яЁё]")
KEYWORD_CARRIER = "подскажите пожалуйста, {}"
SERVICE_TEMPLATES = ("{}", "сколько стоит {}", "что такое {}", "хочу записаться на {}", "{} противопоказания")
TOPIC_CARRIER = "у меня {}"
HOMOGLYPHS = str.maketrans({"а": "a", "е": "e", "о": "o", "с": "c", "р": "p", "х": "x", "у": "y"})
MUTATED_SOURCES = ("evals", "tests", "live", "curated")
# Нагрузка на машину (22.09: 8 процессов на всех ядрах раскрутили вентилятор Mac mini). Объём прогона
# тот же, просто медленнее: по 2 процесса на версию, пониженный приоритет и паузы — каждый процесс
# работает ~WORK_SLICE секунд, потом отдыхает PAUSE_RATIO от этого времени.
DEFAULT_WORKERS = 2
CHILD_NICENESS = 10
WORK_SLICE_SECONDS = 2.0
DEFAULT_PAUSE_RATIO = 0.5


# ---------------------------------------------------------------- корпус


def message_id(message: str) -> str:
    return "m-" + hashlib.sha1(normalize_message(message).encode("utf-8")).hexdigest()[:12]


def normalize_message(message: str) -> str:
    return " ".join(message.split())


class CorpusBuilder:
    """собирает уникальные сообщения; у сообщения из нескольких источников все источники в списке."""

    def __init__(self) -> None:
        self.cases: dict[str, dict[str, Any]] = {}

    def add(self, message: Any, source: str) -> None:
        if not isinstance(message, str):
            return
        text = normalize_message(message)
        if len(text) < 2 or len(text) > 400:
            return
        case_id = message_id(text)
        case = self.cases.setdefault(case_id, {"id": case_id, "message": text, "sources": []})
        if source not in case["sources"]:
            case["sources"].append(source)

    def add_many(self, messages: Iterable[Any], source: str) -> None:
        for message in messages:
            self.add(message, source)

    def add_override(self, message: Any, override: dict[str, Any], source: str, note: str = "") -> None:
        """сообщение с заданной классификацией — отдельный случай (свой id), не сливается с обычным."""

        if not isinstance(message, str) or not normalize_message(message):
            return
        text = normalize_message(message)
        key = text + "\u0000" + json.dumps(override, ensure_ascii=False, sort_keys=True)
        case_id = "o-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
        case = self.cases.setdefault(
            case_id, {"id": case_id, "message": text, "sources": [], "classification_override": override, "note": note}
        )
        if source not in case["sources"]:
            case["sources"].append(source)


def _strings_under_keys(payload: Any, keys: tuple[str, ...]) -> list[str]:
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            if any(marker in str(key).lower() for marker in keys):
                if isinstance(value, str):
                    found.append(value)
                elif isinstance(value, list):
                    found.extend(item for item in value if isinstance(item, str))
            found.extend(_strings_under_keys(value, keys))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_strings_under_keys(item, keys))
    return found


def _messages_from_tests(tests_dir: Path) -> list[str]:
    """кириллические строки, которые тесты подают боту: позиционные аргументы вызовов (сообщение часто
    идёт вторым — _chat(client, "…")), message=..., {"message": ...}. Немного шума (ожидаемые тексты)
    допустим: это тоже валидные входы для бота."""

    found: list[str] = []

    def keep(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and CYRILLIC.search(node.value):
            if "\n" not in node.value and 2 < len(node.value) < 200:
                found.append(node.value)

    for path in sorted(tests_dir.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for argument in node.args:
                    keep(argument)
                for keyword in node.keywords:
                    if keyword.arg == "message":
                        keep(keyword.value)
            elif isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values):
                    if isinstance(key, ast.Constant) and key.value == "message":
                        keep(value)
    return found


def _list_literal(path: Path, name: str) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return []
            return [item for item in value if isinstance(item, str)]
    return []


def build_corpus(repo_dir: Path, clients_dir: Path, eval_dir: Path, company_id: str) -> list[dict[str, Any]]:
    """корпус из кода и данных ОДНОЙ версии (repo_dir) + приватных файлов eval/. Детерминирован."""

    import yaml

    from app.knowledge import KnowledgeBaseResolver
    from app.policy import constants

    builder = CorpusBuilder()
    backend_dir = repo_dir / "backend"

    for path in sorted((backend_dir / "evals").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    builder.add(json.loads(line).get("message"), f"evals:{path.stem}")
                except json.JSONDecodeError:
                    continue
    for path in sorted((repo_dir / "evals").rglob("*.yaml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        builder.add_many(_strings_under_keys(payload, ("message",)), f"evals:{path.stem}")
    builder.add_many(_messages_from_tests(backend_dir / "tests"), "tests")
    builder.add_many(_list_literal(backend_dir / "scripts" / "debug_trace_batch.py", "DEFAULT_CASES"), "tests:debug_trace")

    for name in sorted(vars(constants)):
        value = getattr(constants, name)
        if not name.isupper() or not isinstance(value, (set, frozenset, list, tuple, dict)) or not value:
            continue
        keywords = sorted(key for key in value if isinstance(key, str))
        if not keywords or len(keywords) != len(value):
            continue
        for keyword in keywords:
            builder.add(keyword, f"keywords:{name}")
            builder.add(KEYWORD_CARRIER.format(keyword), f"keywords:{name}")

    resolver = KnowledgeBaseResolver(
        data_dir=backend_dir / "data",
        clients_data_dir=clients_dir,
        defaults_data_dir=backend_dir / "data" / "defaults",
        default_company_id=company_id,
    )
    kb = resolver.get(company_id, fallback=False)
    for service in kb.services:
        for label in [service.name, *service.synonyms]:
            for template in SERVICE_TEMPLATES:
                builder.add(template.format(label.lower()), "client:services")
    builder.add_many((item.question for item in kb.quick_faq), "client:faq")
    for entry in kb.article_service_map.values():
        builder.add(entry.title, "client:articles")
        builder.add_many(entry.trigger_phrases, "client:articles")
    for keyword in _strings_under_keys(kb.config_payload, ("keyword", "trigger")):
        builder.add(keyword, "client:config")
        builder.add(TOPIC_CARRIER.format(keyword), "client:config")

    for path in sorted(eval_dir.glob("*.jsonl")) if eval_dir.exists() else []:
        kind = "live" if path.stem.startswith("live") else "curated"
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            override = row.get("classification_override")
            if isinstance(override, dict) and override:
                builder.add_override(row.get("message"), override, f"{kind}:{path.stem}", row.get("note", ""))
            else:
                builder.add(row.get("message"), f"{kind}:{path.stem}")

    # искажения: заглавные и латиница вместо похожих кириллических букв (был живой баг с подменой)
    originals = [
        case
        for case in builder.cases.values()
        if "classification_override" not in case and any(s.split(":")[0] in MUTATED_SOURCES for s in case["sources"])
    ]
    for case in originals:
        builder.add(case["message"].upper(), "mutation:caps")
        builder.add(case["message"].lower().translate(HOMOGLYPHS), "mutation:homoglyph")

    return sorted(builder.cases.values(), key=lambda case: case["id"])


# ---------------------------------------------------------------- прогон одной версии


def _freeze_randomness_and_time(moment: dict[str, datetime]) -> None:
    import app.hours as hours

    random.choice = lambda seq: seq[0]  # type: ignore[assignment]

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D401
            current = moment["value"]
            return current.astimezone(tz) if tz is not None else current.replace(tzinfo=None)

    hours.datetime = FixedDatetime  # type: ignore[attr-defined]


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str, sort_keys=True))


def _hash(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# Из словаря фраз, который лежит в safe_context целиком, модель и проверка ответов читают только эти
# ключи (grep phrasebook.get в llm/ и validator.py, 2026-09-23). Весь словарь в отпечаток не берём:
# новая фраза, которую никто не читает, иначе давала тысячи ложных различий (так было 23.09).
PHRASEBOOK_KEYS_READ_BY_LLM = ("price_disclaimer",)


def _context_hash(context: dict[str, Any]) -> str:
    """всё, что правила передают дальше (проверке, LLM, chat_service), кроме словаря фраз целиком."""

    phrasebook = context.get("phrasebook") if isinstance(context.get("phrasebook"), dict) else {}
    relevant = {key: value for key, value in context.items() if key != "phrasebook"}
    relevant["phrasebook_read_keys"] = {key: phrasebook.get(key) for key in PHRASEBOOK_KEYS_READ_BY_LLM}
    return _hash(relevant)


def _llm_input_hash(llm_client_cls: Any, context: dict[str, Any]) -> str | None:
    """выжимка контекста, которую эта версия кода реально отправила бы в Yandex (_context_for_model)."""

    if llm_client_cls is None:
        return None
    try:
        return _hash(llm_client_cls(api_key="snapshot")._context_for_model(context))
    except Exception as error:  # noqa: BLE001 — другая версия кода может не уметь; это не повод падать
        return f"error:{type(error).__name__}"


def run_version(
    app_dir: Path,
    corpus: list[dict[str, Any]],
    clients_dir: Path,
    company_id: str,
    times: tuple[str, ...],
    coverage: bool = False,
    pause_ratio: float = 0.0,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """прогоняет корпус через версию кода из app_dir (backend/ этой версии). Вызывается в отдельном процессе."""

    from app.config import Settings
    from app.knowledge import KnowledgeBaseResolver
    from app.llm.mock import MockLLMClient

    try:
        from app.llm.openai_compatible import OpenAIClient as llm_client_cls
    except ImportError:
        llm_client_cls = None
    from app.models import Message, MessageRole, Session
    from app.policy import analyze_message
    from app.routes.chat_utils import resolve_classification

    moment = {"value": FIXED_TIMES["day"]}
    _freeze_randomness_and_time(moment)
    # без .env: базовая версия разворачивается в папке без него, рабочая копия его видит — настройки
    # (например LLM_USE_STRUCTURED_CLASSIFIER) иначе разошлись бы не из-за кода
    settings = Settings(_env_file=None)
    resolver = KnowledgeBaseResolver(
        data_dir=app_dir / "data",
        clients_data_dir=clients_dir,
        defaults_data_dir=app_dir / "data" / "defaults",
        default_company_id=company_id,
    )
    kb = resolver.get(company_id, fallback=False)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=settings, llm_client=MockLLMClient(), knowledge_base=kb, system_prompt="")
        )
    )

    tracer = _CoreCoverage(app_dir) if coverage else None
    setups = _context_setups(kb)
    loop = asyncio.new_event_loop()
    results: list[dict[str, Any]] = []
    slice_started = time_module.perf_counter()
    try:
        for case in corpus:
            worked = time_module.perf_counter() - slice_started
            if pause_ratio > 0 and worked >= WORK_SLICE_SECONDS:
                time_module.sleep(worked * pause_ratio)  # передышка процессору, на результат не влияет
                slice_started = time_module.perf_counter()
            message = case["message"]
            for context_name in contexts_for(message):
                setup = setups[context_name]
                session_id = f"snap-{case['id']}-{context_name}"
                # классификация от часов не зависит (время проверяется только в правилах и кнопках) —
                # считаем один раз на сообщение и контекст, это ~60% времени прогона
                try:
                    classification = loop.run_until_complete(
                        resolve_classification(
                            message, request, kb,
                            _new_session(Session, Message, MessageRole, company_id, session_id, message, setup),
                        )
                    )
                    classification_error = None
                except Exception as error:  # noqa: BLE001
                    classification, classification_error = None, f"classification {type(error).__name__}: {error}"[:300]
                # «что если Yandex сказал X»: ветки, куда локальный классификатор не заводит (curated-случаи)
                if classification is not None and isinstance(case.get("classification_override"), dict):
                    classification = {**classification, **case["classification_override"]}
                for time_label in times:
                    moment["value"] = FIXED_TIMES[time_label]
                    record: dict[str, Any] = {"id": case["id"], "context": context_name, "time": time_label}
                    if classification_error:
                        record["error"] = classification_error
                        results.append(record)
                        continue
                    session = _new_session(Session, Message, MessageRole, company_id, session_id, message, setup)
                    try:
                        if tracer:
                            tracer.start()
                        try:
                            result = analyze_message(message, session, kb, copy.deepcopy(classification))
                        finally:
                            if tracer:
                                tracer.stop()
                        context = result.safe_context or {}
                        record.update(
                            classification=_jsonable(classification),
                            action=getattr(result.action, "value", result.action),
                            reason=getattr(result.reason, "value", result.reason),
                            service_id=result.service_id,
                            confidence=round(float(result.confidence or 0.0), 3),
                            quick_actions=_jsonable(result.quick_actions),
                            question_type=context.get("question_type"),
                            message_to_user=context.get("message_to_user"),
                            context_hash=_context_hash(context),
                            llm_input_hash=_llm_input_hash(llm_client_cls, context),
                            error=None,
                        )
                    except Exception as error:  # noqa: BLE001 — падение на одном сообщении — тоже результат
                        record["error"] = f"{type(error).__name__}: {error}"[:300]
                    results.append(record)
    finally:
        loop.close()
    return results, tracer.report() if tracer else None


def _context_setups(kb: Any) -> dict[str, dict[str, Any]]:
    """как выглядит сессия перед сообщением в каждом контексте: поля сессии + предыдущий обмен репликами."""

    from app.models import ContextFrame

    priced = sorted(
        (service for service in kb.services if kb.get_service_context(service).get("price")),
        key=lambda service: service.id,
    )
    ordered = priced or sorted(kb.services, key=lambda item: item.id)
    service = ordered[0] if ordered else None
    service_name = service.name.lower() if service else "услуга"
    # «рамка» разговора, как её ставит chat_service после ответа об услуге / о нескольких процедурах:
    # по ней классификатор резолвит продолжения («а сколько?», «записаться»)
    service_frame = (
        ContextFrame(frame_type="service_interest", entity_type="service", entity_id=service.id,
                     entity_label=service.name, last_intent="price_question", expires_at_turn=10)
        if service else None
    )
    candidates = [{"id": item.id, "name": item.name} for item in ordered[:2]]
    candidates_frame = ContextFrame(
        frame_type="cosmetic_candidates", slots={"candidates": candidates}, last_intent="cosmetic_concern", expires_at_turn=10
    )
    return {
        "fresh": {"fields": {}, "history": []},
        "offered_operator": {
            "fields": {"pending_action": "offered_operator", "last_intent": "regulated_advice"},
            "history": [("user", "у меня болит спина, что делать"), ("assistant", "Тут лучше подскажет специалист. Подключить менеджера?")],
        },
        "collect_contact": {
            "fields": {"pending_action": "collect_contact", "last_intent": "lead_request"},
            "history": [("user", "хочу, чтобы мне перезвонили"), ("assistant", "Оставьте, пожалуйста, имя и телефон — менеджер перезвонит.")],
        },
        "booking_contact": {
            "fields": {"pending_action": "booking_contact", "last_intent": "booking_request", "last_service_id": service.id if service else None},
            "history": [("user", f"хочу записаться на {service_name}"), ("assistant", "Чтобы записать вас, оставьте имя и телефон.")],
        },
        "service_context": {
            "fields": {
                "last_service_id": service.id if service else None,
                "last_intent": "price_question",
                "active_frame": service_frame,
            },
            "history": [("user", f"сколько стоит {service_name}"), ("assistant", f"Цена на {service_name} зависит от варианта.")],
        },
        "cosmetic_candidates": {
            "fields": {"last_intent": "cosmetic_concern", "active_frame": candidates_frame},
            "history": [
                ("user", "у меня акне, что посоветуете?"),
                ("assistant", "Обычно рассматривают: " + ", ".join(item["name"] for item in candidates) + "."),
            ],
        },
        "lead_given": {
            "fields": {"lead_requested": True, "last_intent": "booking_request"},
            "history": [("user", "меня зовут Анна, телефон <phone>"), ("assistant", "Спасибо, заявку передали.")],
        },
        "operator_requested": {
            "fields": {"operator_requested": True, "status": "WAITING_OPERATOR", "last_intent": "operator_request"},
            "history": [("user", "позовите менеджера"), ("assistant", "Передала менеджеру, он скоро подключится.")],
        },
    }


def _new_session(session_cls, message_cls, role_cls, company_id: str, session_id: str, message: str, setup=None):
    session = session_cls(company_id=company_id, session_id=session_id)
    setup = setup or {"fields": {}, "history": []}
    for role, text in setup["history"]:
        session.messages.append(message_cls(role=role_cls(role), text=text))
    for field, value in setup["fields"].items():
        if field == "status":
            value = type(session.status)(value)  # SessionStatus: у поля есть .value, строка не подойдёт
        setattr(session, field, value)
    session.messages.append(message_cls(role=role_cls.USER, text=message))
    session.message_count = sum(1 for item in session.messages if item.role == role_cls.USER)
    return session


def contexts_for(message: str) -> tuple[str, ...]:
    return CONTEXTS if len(message.split()) <= FOLLOWUP_MAX_WORDS else ("fresh",)


class _CoreCoverage:
    """какие return внутри _analyze_message_core выполнились (sys.settrace только вокруг analyze_message)."""

    def __init__(self, app_dir: Path) -> None:
        self.path = (app_dir / "app" / "policy" / "__init__.py").resolve()
        source = self.path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        core = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_analyze_message_core")
        self.returns: dict[int, str] = {}
        for node in ast.walk(core):
            if isinstance(node, ast.Return):
                segment = ast.get_source_segment(source, node) or ""
                reason = re.search(r"PolicyReason\.(\w+)", segment)
                self.returns[node.lineno] = reason.group(1) if reason else _enclosing_hint(core, node)
        self.hits: set[int] = set()
        self._target = str(self.path)

    def _local(self, frame, event, arg):
        if event == "line":
            self.hits.add(frame.f_lineno)
        return self._local

    def _global(self, frame, event, arg):
        if frame.f_code.co_filename == self._target and frame.f_code.co_name == "_analyze_message_core":
            return self._local
        return None

    def start(self) -> None:
        sys.settrace(self._global)

    def stop(self) -> None:
        sys.settrace(None)

    def report(self) -> dict[str, Any]:
        missing = {line: hint for line, hint in sorted(self.returns.items()) if line not in self.hits}
        return {
            "returns_total": len(self.returns),
            "returns_hit": len(self.returns) - len(missing),
            "missing": [{"line": line, "hint": hint} for line, hint in missing.items()],
            "returns": {str(line): hint for line, hint in sorted(self.returns.items())},
            "hit_lines": sorted(line for line in self.hits if line in self.returns),
        }


def _enclosing_hint(core: ast.FunctionDef, target: ast.Return) -> str:
    """для return без явной PolicyReason — что возвращается (имя переменной/вызова)."""

    value = target.value
    if isinstance(value, ast.Name):
        return f"return {value.id}"
    if isinstance(value, ast.Call):
        func = value.func
        return f"return {getattr(func, 'id', getattr(func, 'attr', 'call'))}(...)"
    return "return"


# ---------------------------------------------------------------- сравнение


def diff_results(base: list[dict[str, Any]], head: list[dict[str, Any]], messages: dict[str, str]) -> dict[str, Any]:
    base_by_key = {(r["id"], r.get("context", "fresh"), r["time"]): r for r in base}
    head_by_key = {(r["id"], r.get("context", "fresh"), r["time"]): r for r in head}
    changed: list[dict[str, Any]] = []
    for key in sorted(base_by_key.keys() & head_by_key.keys()):
        old, new = base_by_key[key], head_by_key[key]
        fields = [field for field in COMPARED_FIELDS if old.get(field) != new.get(field)]
        if fields:
            changed.append(
                {
                    "id": key[0],
                    "context": key[1],
                    "time": key[2],
                    "message": messages.get(key[0], ""),
                    "fields": fields,
                    "before": {field: old.get(field) for field in fields},
                    "after": {field: new.get(field) for field in fields},
                    "transition": f"{old.get('action')}/{old.get('reason')} → {new.get('action')}/{new.get('reason')}",
                }
            )
    return {
        "compared": len(base_by_key.keys() & head_by_key.keys()),
        "only_base": sorted(f"{i}@{c}@{t}" for i, c, t in base_by_key.keys() - head_by_key.keys()),
        "only_head": sorted(f"{i}@{c}@{t}" for i, c, t in head_by_key.keys() - base_by_key.keys()),
        "errors_base": sum(1 for r in base if r.get("error")),
        "errors_head": sum(1 for r in head if r.get("error")),
        "changed": changed,
    }


def load_expectations(path: Path | None) -> set[str]:
    """файл ожиданий: {"ids": ["m-…", "m-…@offered_operator", "m-…@fresh@night"], "note": "…"} или просто список.
    id без уточнений — все контексты и оба времени."""

    if path is None:
        return set()
    payload = json.loads(path.read_text(encoding="utf-8"))
    ids = payload.get("ids", []) if isinstance(payload, dict) else payload
    return {str(item) for item in ids}


def _expected(change: dict[str, Any], expected: set[str]) -> bool:
    return (
        change["id"] in expected
        or f"{change['id']}@{change['context']}" in expected
        or f"{change['id']}@{change['context']}@{change['time']}" in expected
    )


def render_report(diff: dict[str, Any], expected: set[str], meta: dict[str, Any], limit: int = 5) -> str:
    changed = diff["changed"]
    unexpected = [c for c in changed if not _expected(c, expected)]
    lines = [
        f"# Снимок правил: {meta['base_label']} → {meta['head_label']}",
        "",
        f"- данные клиента: `{meta['company_id']}` @ {meta['data_commit']}",
        f"- корпус: {meta['corpus_size']} сообщений; короткие (до {FOLLOWUP_MAX_WORDS} слов) ещё в {len(CONTEXTS) - 1} состояниях диалога; "
        f"время: {', '.join(meta['times'])}",
        f"- сравнено: {diff['compared']}, изменилось: {len(changed)}, из них неожиданных: {len(unexpected)}",
        f"- ошибок: было {diff['errors_base']}, стало {diff['errors_head']}",
        f"- время прогона: база {meta['base_seconds']} с, новая версия {meta['head_seconds']} с",
        "",
    ]
    if not changed:
        lines.append("Различий нет.")
        return "\n".join(lines) + "\n"
    by_field = Counter(field for change in changed for field in change["fields"])
    lines += ["## Что менялось", ""] + [f"- `{field}`: {count}" for field, count in by_field.most_common()] + [""]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for change in changed:
        groups[change["transition"]].append(change)
    lines += ["## По переходам (действие/причина)", ""]
    for transition, items in sorted(groups.items(), key=lambda item: -len(item[1])):
        flagged = sum(1 for c in items if not _expected(c, expected))
        lines.append(f"### {transition} — {len(items)} (неожиданных {flagged})")
        for change in items[:limit]:
            mark = "✓" if _expected(change, expected) else "✗"
            lines.append(
                f"- {mark} `{change['id']}@{change['context']}@{change['time']}` «{change['message'][:90]}» — {', '.join(change['fields'])}"
            )
        if len(items) > limit:
            lines.append(f"- … ещё {len(items) - limit}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- оркестровка


def _git(*args: str, cwd: Path = REPO_DIR) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _child_env(clients_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "LLM_PROVIDER": "mock",
            "LLM_API_KEY": "",
            "DEV_MODE": "true",
            "CLIENTS_DATA_DIR": str(clients_dir),
            "RAG_CHUNKS_FILE": os.environ.get("RAG_CHUNKS_FILE", str(DEFAULT_RAG_FILE)),
            "LOG_LEVEL": "ERROR",
            "TELEGRAM_BOT_TOKEN": "",
            # pymorphy2 шумит DeprecationWarning про pkg_resources в каждом процессе
            "PYTHONWARNINGS": "ignore",
        }
    )
    return env


def _python() -> str:
    """дочерним процессам нужны зависимости проекта: берём .venv, даже если сам скрипт запущен системным python3."""

    venv_python = REPO_DIR / ".venv" / "bin" / "python"
    return str(venv_python) if venv_python.exists() else sys.executable


def _lower_priority() -> None:
    """для дочерних процессов: система отдаёт им процессор в последнюю очередь."""

    try:
        os.nice(CHILD_NICENESS)
    except (AttributeError, OSError):
        pass


PREEXEC = _lower_priority if os.name == "posix" else None


def _run_child(command: str, repo_dir: Path, args: list[str], clients_dir: Path) -> float:
    started = time_module.perf_counter()
    subprocess.run(
        [_python(), str(SCRIPT_PATH), command, "--repo-dir", str(repo_dir), *args],
        env=_child_env(clients_dir),
        check=True,
        preexec_fn=PREEXEC,
    )
    return round(time_module.perf_counter() - started, 1)


def _run_versions_parallel(
    jobs: list[tuple[str, Path]],
    corpus_path: Path,
    out_dir: Path,
    common: list[str],
    times: str,
    workers: int,
    clients_dir: Path,
    pause_ratio: float,
) -> dict[str, float]:
    """все версии и все их доли корпуса одновременно; результат каждой версии склеивается в results_<версия>.jsonl."""

    started = time_module.perf_counter()
    running: list[tuple[str, subprocess.Popen, Path]] = []
    for label, repo_dir in jobs:
        for shard in range(workers):
            part = out_dir / f"results_{label}.part{shard}.jsonl"
            command = [
                _python(), str(SCRIPT_PATH), "_run", "--repo-dir", str(repo_dir), *common,
                "--corpus", str(corpus_path), "--times", times, "--out", str(part), "--shard", f"{shard}/{workers}",
                "--pause", str(pause_ratio),
            ]
            process = subprocess.Popen(command, env=_child_env(clients_dir), preexec_fn=PREEXEC)
            running.append((label, process, part))
    finished: dict[str, float] = {}
    failed = []
    for label, process, part in running:
        if process.wait() != 0:
            failed.append(f"{label}:{part.name}")
        finished[label] = round(time_module.perf_counter() - started, 1)
    if failed:
        raise RuntimeError(f"прогон упал: {', '.join(failed)}")
    for label, _repo_dir in jobs:
        parts = sorted(out_dir.glob(f"results_{label}.part*.jsonl"))
        rows = [row for part in parts for row in _read_jsonl(part)]
        _write_jsonl(out_dir / f"results_{label}.jsonl", rows)
        for part in parts:
            part.unlink()
    return finished


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _data_commit(clients_dir: Path) -> str:
    try:
        commit = _git("rev-parse", "--short", "HEAD", cwd=clients_dir)
        dirty = _git("status", "--porcelain", cwd=clients_dir)
    except (subprocess.CalledProcessError, FileNotFoundError, NotADirectoryError):
        return "не git"
    return commit + (" (есть незакоммиченные правки)" if dirty else "")


def command_compare(args: argparse.Namespace) -> int:
    clients_dir = Path(args.clients_dir).resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base_label = args.base
    head_label = f"рабочая копия ({_git('rev-parse', '--abbrev-ref', 'HEAD')}@{_git('rev-parse', '--short', 'HEAD')})"
    out_dir = Path(args.out_dir) / f"{stamp}_{re.sub(r'[^A-Za-z0-9._-]', '_', base_label)}"
    out_dir.mkdir(parents=True, exist_ok=True)
    worktree = Path(tempfile.mkdtemp(prefix="policy-snapshot-base-"))
    shutil.rmtree(worktree)
    _git("worktree", "add", "--detach", "--quiet", str(worktree), args.base)
    try:
        common = ["--clients-dir", str(clients_dir), "--eval-dir", str(args.eval_dir), "--company", args.company]
        for label, repo_dir in (("base", worktree), ("head", REPO_DIR)):
            _run_child("_corpus", repo_dir, [*common, "--out", str(out_dir / f"corpus_{label}.jsonl")], clients_dir)
        corpus: dict[str, dict[str, Any]] = {}
        for label in ("base", "head"):
            for case in _read_jsonl(out_dir / f"corpus_{label}.jsonl"):
                existing = corpus.setdefault(case["id"], case)
                for source in case["sources"]:
                    if source not in existing["sources"]:
                        existing["sources"].append(source)
        corpus_path = out_dir / "corpus.jsonl"
        _write_jsonl(corpus_path, sorted(corpus.values(), key=lambda case: case["id"]))
        seconds = _run_versions_parallel(
            [("base", worktree), ("head", REPO_DIR)],
            corpus_path,
            out_dir,
            common,
            ",".join(args.times),
            args.workers,
            clients_dir,
            args.pause,
        )
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=REPO_DIR, capture_output=True)

    diff = diff_results(
        _read_jsonl(out_dir / "results_base.jsonl"),
        _read_jsonl(out_dir / "results_head.jsonl"),
        {case_id: case["message"] for case_id, case in corpus.items()},
    )
    expected = load_expectations(Path(args.expect) if args.expect else None)
    meta = {
        "base_label": base_label,
        "head_label": head_label,
        "company_id": args.company,
        "data_commit": _data_commit(clients_dir),
        "corpus_size": len(corpus),
        "times": args.times,
        "base_seconds": seconds["base"],
        "head_seconds": seconds["head"],
    }
    report = render_report(diff, expected, meta)
    (out_dir / "report.md").write_text(report, encoding="utf-8")
    (out_dir / "diff.json").write_text(json.dumps({"meta": meta, **diff}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(report)
    print(f"Файлы: {out_dir}")
    unexpected = [c for c in diff["changed"] if not _expected(c, expected)]
    new_errors = diff["errors_head"] > diff["errors_base"]
    return 1 if unexpected or new_errors or diff["only_base"] or diff["only_head"] else 0


def command_coverage(args: argparse.Namespace) -> int:
    """корпус через рабочую копию с трассировкой _analyze_message_core, частями параллельно; попадания складываются."""

    clients_dir = Path(args.clients_dir).resolve()
    out_dir = Path(args.out_dir) / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}_coverage"
    out_dir.mkdir(parents=True, exist_ok=True)
    common = ["--clients-dir", str(clients_dir), "--eval-dir", str(args.eval_dir), "--company", args.company]
    _run_child("_corpus", REPO_DIR, [*common, "--out", str(out_dir / "corpus.jsonl")], clients_dir)
    running = []
    for shard in range(args.workers):
        command = [
            _python(), str(SCRIPT_PATH), "_run", "--repo-dir", str(REPO_DIR), *common,
            "--corpus", str(out_dir / "corpus.jsonl"), "--times", ",".join(args.times),
            "--out", str(out_dir / f"results.part{shard}.jsonl"),
            "--coverage-out", str(out_dir / f"coverage.part{shard}.json"),
            "--shard", f"{shard}/{args.workers}", "--pause", str(args.pause),
        ]
        running.append(subprocess.Popen(command, env=_child_env(clients_dir), preexec_fn=PREEXEC))
    if any(process.wait() != 0 for process in running):
        print("прогон упал", file=sys.stderr)
        return 2
    parts = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(out_dir.glob("coverage.part*.json"))]
    returns = {int(line): hint for line, hint in parts[0]["returns"].items()}
    hit = set().union(*(set(part["hit_lines"]) for part in parts))
    missing = [{"line": line, "hint": hint} for line, hint in sorted(returns.items()) if line not in hit]
    report = {"returns_total": len(returns), "returns_hit": len(returns) - len(missing), "missing": missing}
    (out_dir / "coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Исходы _analyze_message_core: задето {report['returns_hit']} из {report['returns_total']}")
    for item in missing:
        print(f"  не задет: строка {item['line']} — {item['hint']}")
    print(f"Файлы: {out_dir}")
    return 0


NAME_HINT = re.compile(r"(меня зовут|моё имя|мое имя|это\s+[А-ЯЁ][а-яё]+)|(?<![.!?]\s)(?<!^)\b[А-ЯЁ][а-яё]{2,}\b")


def command_import_live(args: argparse.Namespace) -> int:
    """сообщения клиентов из выгрузки /backstage → eval/live_messages.jsonl (телефоны уже скрыты выгрузкой)."""

    eval_dir = Path(args.eval_dir)
    eval_dir.mkdir(parents=True, exist_ok=True)
    target = eval_dir / "live_messages.jsonl"
    seen = {row["message"] for row in _read_jsonl(target)} if target.exists() else set()
    added = flagged = 0
    rows = _read_jsonl(target) if target.exists() else []
    for path in args.files:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        for chat in payload.get("conversations", []):
            for message in chat.get("messages", []):
                text = normalize_message(str(message.get("text") or ""))
                if message.get("role") != "user" or len(text) < 2 or text in seen:
                    continue
                seen.add(text)
                check_name = bool(NAME_HINT.search(text))
                rows.append({"message": text, "source": f"live:{str(chat.get('session_id'))[:8]}", "check_name": check_name})
                added += 1
                flagged += int(check_name)
    _write_jsonl(target, rows)
    print(f"Добавлено сообщений: {added}, всего в файле: {len(rows)} → {target}")
    print(f"Проверить глазами на имена (check_name=true): {flagged}")

    # переписки целиком (только реплики клиента) — для слоя Б, dialog_snapshot.py
    dialogs_target = eval_dir / "live_dialogs.jsonl"
    dialogs = _read_jsonl(dialogs_target) if dialogs_target.exists() else []
    known = {tuple(row["turns"]) for row in dialogs}
    added_dialogs = 0
    for path in args.files:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        for chat in payload.get("conversations", []):
            turns = [
                normalize_message(str(message.get("text") or ""))
                for message in chat.get("messages", [])
                if message.get("role") == "user" and normalize_message(str(message.get("text") or ""))
            ]
            if turns and tuple(turns) not in known:
                known.add(tuple(turns))
                dialogs.append({"turns": turns, "source": f"live:{str(chat.get('session_id'))[:8]}"})
                added_dialogs += 1
    _write_jsonl(dialogs_target, dialogs)
    print(f"Переписок добавлено: {added_dialogs}, всего: {len(dialogs)} → {dialogs_target}")
    return 0


def _child_setup(args: argparse.Namespace) -> Path:
    repo_dir = Path(args.repo_dir).resolve()
    sys.path.insert(0, str(repo_dir / "backend"))
    # откаты классификатора на локальный результат (Mock) пишутся WARNING на каждое сообщение — шум
    logging.getLogger("app").setLevel(logging.ERROR)
    return repo_dir


def command_child_corpus(args: argparse.Namespace) -> int:
    repo_dir = _child_setup(args)
    corpus = build_corpus(repo_dir, Path(args.clients_dir), Path(args.eval_dir), args.company)
    _write_jsonl(Path(args.out), corpus)
    return 0


def command_child_run(args: argparse.Namespace) -> int:
    repo_dir = _child_setup(args)
    corpus = _read_jsonl(Path(args.corpus))
    shard, total = (int(part) for part in args.shard.split("/"))
    corpus = corpus[shard::total]
    results, coverage = run_version(
        repo_dir / "backend",
        corpus,
        Path(args.clients_dir),
        args.company,
        tuple(args.times.split(",")),
        coverage=bool(args.coverage_out),
        pause_ratio=args.pause,
    )
    _write_jsonl(Path(args.out), results)
    if args.coverage_out:
        Path(args.coverage_out).write_text(json.dumps(coverage, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--clients-dir", default=os.environ.get("SNAPSHOT_CLIENTS_DIR", str(DEFAULT_CLIENTS_DIR)))
        p.add_argument("--eval-dir", default=os.environ.get("SNAPSHOT_EVAL_DIR", str(DEFAULT_EVAL_DIR)))
        p.add_argument("--company", default=DEFAULT_COMPANY)

    compare = sub.add_parser("compare", help="база против рабочей копии")
    common(compare)
    compare.add_argument("--base", default=DEFAULT_BASE, help="git-ссылка базовой версии")
    compare.add_argument("--expect", help="файл ожидаемых изменений (id случаев)")
    compare.add_argument("--times", nargs="+", default=list(FIXED_TIMES), choices=list(FIXED_TIMES))
    compare.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    compare.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="процессов на версию (по умолчанию 2)")
    compare.add_argument(
        "--pause", type=float, default=DEFAULT_PAUSE_RATIO, help="доля отдыха от времени работы (0 — без пауз, 0.5 по умолчанию)"
    )

    coverage = sub.add_parser("coverage", help="покрытие исходов _analyze_message_core корпусом")
    common(coverage)
    coverage.add_argument("--times", nargs="+", default=list(FIXED_TIMES), choices=list(FIXED_TIMES))
    coverage.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    # одна версия, поэтому процессов вдвое больше, чем на версию в compare, — общая нагрузка та же
    coverage.add_argument("--workers", type=int, default=DEFAULT_WORKERS * 2)
    coverage.add_argument("--pause", type=float, default=DEFAULT_PAUSE_RATIO)

    live = sub.add_parser("import-live", help="сообщения клиентов из выгрузки /backstage в eval/")
    common(live)
    live.add_argument("files", nargs="+")

    for name in ("_corpus", "_run"):  # внутренние: выполняются в процессе нужной версии кода
        child = sub.add_parser(name)
        common(child)
        child.add_argument("--repo-dir", required=True)
        child.add_argument("--out", required=True)
        if name == "_run":
            child.add_argument("--corpus", required=True)
            child.add_argument("--times", default="day,night")
            child.add_argument("--coverage-out")
            child.add_argument("--shard", default="0/1", help="i/N — какую долю корпуса гонять")
            child.add_argument("--pause", type=float, default=0.0)

    args = parser.parse_args(argv)
    handlers = {
        "compare": command_compare,
        "coverage": command_coverage,
        "import-live": command_import_live,
        "_corpus": command_child_corpus,
        "_run": command_child_run,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
