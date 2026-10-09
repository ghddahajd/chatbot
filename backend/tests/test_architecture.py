"""сторожа устройства кода — чтобы после рефакторинга он снова не сросся в один клубок:
1. правила и датчики только решают: ничего не отправляют и не сохраняют. Иначе их нельзя прогнать
   в отладке или рядом со старой версией (теневой режим);
2. у каждого правила есть фраза, на которой оно срабатывает;
3. файлы логики не растут: большие держим на нынешнем размере и снижаем потолок по ходу
   рефакторинга, остальные — не больше 600 строк."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app import policy
from app.policy.engine import Rule

from .test_policy import _copy_rosh_import_kb

APP = Path(__file__).resolve().parents[1] / "app"

# ---------------------------------------------------------------- 1. правила только решают

SIDE_EFFECT_MODULES = (
    "telegram_bridge", "sessions", "leads", "delivery", "analytics", "ws_manager", "ops_bot", "watchdog",
    "routes", "services.chat_service", "services.session_summarizer",
    "llm.base", "llm.mock", "llm.openai_compatible",
    "httpx", "requests", "urllib", "socket",
)


def _imported_modules(path: Path) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.append(node.module or "")
    return modules


def test_policy_layer_only_decides() -> None:
    offenders = [
        f"{path.relative_to(APP)} → {module}"
        for path in sorted((APP / "policy").rglob("*.py"))
        for module in _imported_modules(path)
        if any(module == name or module.startswith(name + ".") for name in SIDE_EFFECT_MODULES)
    ]
    assert offenders == [], "внешние действия — в обработчике чата, не в правилах"


# ---------------------------------------------------------------- 2. у каждого правила есть пример

# правило → (фраза, ответ классификатора). Новое правило без строки здесь роняет тест ниже.
RULE_EXAMPLES = {
    "crisis": ("я не хочу больше жить", "unknown"),
    "ambulance_fact": ("как вызвать скорую", "unknown"),
    "sensitive_topic": ("можно ли сделать у вас аборт", "regulated_advice"),
    "complaint": ("хочу пожаловаться на врача", "regulated_advice"),
    "medical": ("у меня болит голова", "medical_advice"),
    "booking_change": ("хочу перенести запись", "booking_request"),
}


def _rule_names() -> list[str]:
    return [
        rule.name
        for value in vars(policy).values()
        if isinstance(value, tuple) and value and all(isinstance(item, Rule) for item in value)
        for rule in value
    ]


def test_every_rule_has_an_example() -> None:
    assert sorted(_rule_names()) == sorted(RULE_EXAMPLES)


@pytest.mark.parametrize(("rule", "example"), sorted(RULE_EXAMPLES.items()))
def test_rule_fires_on_its_example(rule: str, example: tuple[str, str], policy_session, resolver, managed_env) -> None:
    knowledge_base = _copy_rosh_import_kb(
        resolver,
        managed_env,
        config_append="""
clinic_info:
  sensitive_topics:
    - keywords: ["аборт"]
      handling: decline
      text: "Такие процедуры в центре не проводятся."
""",
    )
    message, intent = example

    result = policy.analyze_message(message, policy_session, knowledge_base, {"intent": intent, "confidence": 0.9})

    assert result.rule == rule


# ---------------------------------------------------------------- 3. файлы логики не растут

LOGIC_DIRS = ("policy", "services", "routes")
NEW_FILE_LIMIT = 600
# нынешний размер больших файлов — потолок. Рефакторинг его снижает; поднять можно только
# осознанно, правкой этой строки в том же коммите
CEILINGS = {
    "policy/__init__.py": 3934,  # +2: шаг 1 чинил цены в старом коде, они переезжают в свой файл на шаге 4
    "services/chat_service.py": 1949,  # +3: тема разговора сбрасывается после списка услуг; оркестратор худеет на шаге 5
    "policy/constants.py": 1122,
    "routes/chat_utils.py": 980,
    "policy/extractors.py": 865,
    "routes/debug.py": 805,
}


@pytest.mark.parametrize("folder", LOGIC_DIRS)
def test_logic_files_do_not_grow(folder: str) -> None:
    too_big = []
    for path in sorted((APP / folder).rglob("*.py")):
        name = str(path.relative_to(APP))
        lines = len(path.read_text(encoding="utf-8").splitlines())
        limit = CEILINGS.get(name, NEW_FILE_LIMIT)
        if lines > limit:
            too_big.append(f"{name}: {lines} строк, потолок {limit}")
    assert too_big == [], "вынесите часть в отдельный модуль по теме"
