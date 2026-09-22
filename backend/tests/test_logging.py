"""логирование: настройка, безопасные события, отсутствие персональных данных и секретов в логах."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from app import logging_setup
from app.logging_setup import configure_logging, log_event, redact_phones
from app.runtime_stats import STATS, RuntimeStats


def test_configure_logging_sets_levels_and_tolerates_bad_level() -> None:
    saved = {name: logging.getLogger(name).level for name in ("app", "httpx", "httpcore")}
    try:
        configure_logging("DEBUG")
        assert logging.getLogger("app").level == logging.DEBUG
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING

        configure_logging("не-уровень")
        assert logging.getLogger("app").level == logging.INFO
    finally:
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)


def test_logging_in_a_clean_process_shows_app_info_and_hides_httpx_urls() -> None:
    """как на проде: чистый процесс без обработчиков. Проверяем итоговый stderr, а не внутренности.

    httpx на INFO пишет URL запроса целиком, а в URL Telegram лежит токен бота."""

    import subprocess
    import sys
    import textwrap

    backend_dir = Path(__file__).resolve().parents[1]
    code = textwrap.dedent(
        """
        import logging
        from app.logging_setup import configure_logging, log_event

        configure_logging("INFO")
        logging.getLogger("httpx").info('HTTP Request: POST https://api.telegram.org/bot123456:SECRET-TOKEN/getUpdates')
        log_event(logging.getLogger("app.probe"), logging.INFO, "probe_event", ok=True)
        logging.getLogger("app.probe").warning("предупреждение приложения")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=backend_dir, capture_output=True, text=True, env={"PYTHONPATH": str(backend_dir)}
    )

    assert result.returncode == 0, result.stderr
    assert "SECRET-TOKEN" not in result.stderr
    assert "probe_event ok=1" in result.stderr
    assert "предупреждение приложения" in result.stderr
    first_line = result.stderr.splitlines()[0]
    assert first_line.split(" ")[0].endswith("Z")  # время в UTC


def test_log_event_formats_key_values_and_blocks_personal_fields(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("app.test_event")
    with caplog.at_level(logging.INFO, logger="app.test_event"):
        log_event(
            logger,
            logging.INFO,
            "sample_event",
            company_id="rosh",
            ok=True,
            missing=None,
            phone="+7 926 555-44-33",
            message="секретный текст",
            note="два  слова\nи перенос = знак",
            long="x" * 200,
        )

    line = caplog.records[0].getMessage()
    assert line.startswith("sample_event company_id=rosh ok=1 missing=- ")
    assert "phone=<blocked>" in line and "message=<blocked>" in line
    assert "555" not in line and "секретный" not in line
    assert "note=два_слова_и_перенос_:_знак" in line
    assert "long=" + "x" * logging_setup.MAX_VALUE_LENGTH in line
    assert "x" * (logging_setup.MAX_VALUE_LENGTH + 1) not in line


def test_log_event_is_silent_when_level_disabled(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("app.test_silent")
    with caplog.at_level(logging.ERROR, logger="app.test_silent"):
        log_event(logger, logging.INFO, "hidden_event", a=1)

    assert caplog.records == []


@pytest.mark.parametrize(
    "text",
    [
        "позвоните на +7 926 555-44-33 завтра",
        "8 (926) 555 44 33",
        "89265554433",
        "+79265554433",
    ],
)
def test_redact_phones_masks_common_formats(text: str) -> None:
    redacted = redact_phones(text)

    assert "<phone>" in redacted
    assert "555" not in redacted


def test_redact_phones_keeps_dates_and_short_numbers() -> None:
    assert redact_phones("запись 2026-09-22 в 12:30, цена 5000 ₽") == "запись 2026-09-22 в 12:30, цена 5000 ₽"


# ---------------------------------------------------------------- события приложения


def _chat(client, message: str, session_id: str | None = None, company_id: str = "rosh_demo") -> dict:
    response = client.post(
        "/api/chat/message",
        json={"company_id": company_id, "session_id": session_id, "message": message},
    )
    assert response.status_code == 200
    return response.json()


def test_dialog_logs_events_without_personal_data(test_client, caplog: pytest.LogCaptureFixture) -> None:
    knowledge_base = test_client.app.state.knowledge_base_resolver.get("rosh_demo", fallback=False)
    knowledge_base.company.working_hours_schedule = {}  # результат не должен зависеть от часов на машине
    messages = [
        "здравствуйте",
        "сколько стоит чистка лица",
        "у меня очень болит живот что принять",
        "запишите меня, меня зовут Ольга Петрова, телефон +7 926 555-44-33",
    ]
    with caplog.at_level(logging.INFO, logger="app"):
        session_id = None
        for message in messages:
            session_id = _chat(test_client, message, session_id).get("session_id", session_id)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert logged.count("chat_turn ") == len(messages)
    assert "lead_saved " in logged
    assert "has_phone=1" in logged
    for private in ("926", "555-44-33", "Ольга", "Петрова", "болит", "чистка", "запишите", "здравствуйте"):
        assert private not in logged, private


def test_chat_turn_volume_stays_small(test_client, caplog: pytest.LogCaptureFixture) -> None:
    """docker хранит только ~30 МБ логов: на ход диалога должно приходиться несколько коротких строк."""

    with caplog.at_level(logging.INFO, logger="app"):
        session_id = None
        for message in ("привет", "какой у вас адрес", "спасибо"):
            session_id = _chat(test_client, message, session_id).get("session_id", session_id)

    records = [record for record in caplog.records if record.name.startswith("app")]
    assert len(records) <= 3 * 5
    assert max(len(record.getMessage()) for record in records) < 400


def test_startup_summary_reports_mode_without_secrets(managed_env, monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    from fastapi.testclient import TestClient

    from app.config import get_settings

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999999:BOT-SECRET-VALUE")
    monkeypatch.setenv("TELEGRAM_PROXY_URL", "http://user:proxy-pass-777@proxy.example:3128")
    monkeypatch.setenv("OPERATOR_TOKEN", "Operator-Secret-Value-abc123")
    monkeypatch.setenv("TELEGRAM_OPERATORS_GROUP_ID", "")  # без группы бридж выключен: сеть не трогаем
    get_settings.cache_clear()
    from app.main import app

    with caplog.at_level(logging.INFO, logger="app"):
        with TestClient(app):
            pass

    line = next(record.getMessage() for record in caplog.records if record.getMessage().startswith("startup_summary"))
    assert "telegram_proxy=1" in line
    assert "operator_token_default=0" in line
    assert "llm_client=MockLLMClient" in line
    assert "clients=3" in line and "code=" in line
    for secret in ("BOT-SECRET-VALUE", "proxy-pass-777", "Operator-Secret-Value-abc123"):
        assert secret not in "\n".join(record.getMessage() for record in caplog.records)


# ---------------------------------------------------------------- LLM, доставка, Telegram


def test_llm_fallback_is_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    from types import SimpleNamespace

    from app.routes.chat_utils import safe_complete

    class Failing:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("llm down")

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(llm_client=Failing(), system_prompt="")))
    with caplog.at_level(logging.INFO, logger="app"):
        asyncio.run(safe_complete(request, {}, "привет", []))

    fallback = [r for r in caplog.records if "complete_source=fallback" in r.getMessage()]
    assert fallback and fallback[0].levelno == logging.WARNING
    assert "привет" not in fallback[0].getMessage()


def test_slow_llm_call_is_logged_and_measured(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    import time

    from app.routes import chat_utils

    STATS.reset()
    monkeypatch.setattr(chat_utils, "LLM_SLOW_MS", 0)
    with caplog.at_level(logging.INFO, logger="app"):
        chat_utils._record_llm_ok("llm.complete", time.perf_counter() - 0.05)

    assert any(r.getMessage().startswith("llm_slow call=llm.complete") for r in caplog.records)
    summary = STATS.summary("llm.")["llm.complete"]
    assert summary["avg_ms"] >= 50
    STATS.reset()


def test_runtime_stats_summary_carries_latency() -> None:
    stats = RuntimeStats()
    stats.record_ok("x", duration_ms=100)
    stats.record_ok("x", duration_ms=300)
    stats.record_ok("x")

    summary = stats.summary()["x"]

    assert summary["avg_ms"] == 200
    assert summary["max_ms"] == 300


def test_preflight_flags_slow_llm(test_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_client.app.state, "llm_client", object())
    STATS.reset()
    STATS.record_ok("llm.complete", duration_ms=20000)

    check = test_client.get(
        "/api/debug/preflight?network=0", headers={"X-Operator-Token": "demo-operator-token"}
    ).json()["checks"]["llm_runtime"]
    STATS.reset()

    assert check["status"] == "degraded"
    assert "медленно" in check["detail"]


def test_delivery_result_is_logged_without_targets(managed_env, resolver, caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    from app.delivery import DeliveryService

    service = DeliveryService(
        outbox_file=tmp_path / "outbox.jsonl",
        knowledge_base_resolver=resolver,
        telegram_bot_token="token",
        telegram_chat_id="chat",
        telegram_dm_enabled=True,
    )
    responses = iter([200, 500])

    async def fake_send(record: dict) -> int:
        return next(responses)

    service._send = fake_send  # type: ignore[method-assign]
    record = {
        "delivery_id": "d1",
        "event_type": "lead_created",
        "company_id": "rosh_demo",
        "destination_type": "webhook",
        "target": "https://secret.example/hook?key=SECRET",
        "attempts": 0,
    }
    with caplog.at_level(logging.INFO, logger="app"):
        asyncio.run(service._dispatch(record))
        asyncio.run(service._dispatch(record))

    results = [r for r in caplog.records if r.getMessage().startswith("delivery_result")]
    assert [r.levelno for r in results] == [logging.INFO, logging.WARNING]
    assert "status=sent" in results[0].getMessage() and "http=200" in results[0].getMessage()
    assert "http=500" in results[1].getMessage()
    assert "SECRET" not in "\n".join(r.getMessage() for r in caplog.records)


def _bridge():
    from app.sessions import SessionStore
    from app.telegram_bridge import TelegramBridgeService

    return TelegramBridgeService(bot_token="token", group_chat_id="-1001", session_store=SessionStore(), ws_manager=None)


def test_telegram_send_failure_is_logged_without_description(caplog: pytest.LogCaptureFixture) -> None:
    bridge = _bridge()

    with caplog.at_level(logging.INFO, logger="app"):
        bridge._record_failure(
            kind="client_lead_card",
            session_id="abcdef1234567890",
            data={"error_code": 400, "description": "Bad Request: текст клиента про болезнь"},
        )

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("telegram_send_failed"))
    assert "kind=client_lead_card" in line and "session=abcdef12" in line and "error_code=400" in line
    assert "болезнь" not in line and "1234567890" not in line


def test_operator_event_is_logged_without_operator_name(caplog: pytest.LogCaptureFixture) -> None:
    from types import SimpleNamespace

    bridge = _bridge()

    class Analytics:
        async def track_event(self, **kwargs):
            return None

    bridge.analytics_service = Analytics()
    session = SimpleNamespace(company_id="rosh_demo", session_id="feedface12345678")

    with caplog.at_level(logging.INFO, logger="app"):
        asyncio.run(bridge._track_operator_event(event_type="operator_claimed", session=session, claimed_by="@ivan_operator"))

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("operator_event"))
    assert "event_type=operator_claimed" in line and "session=feedface" in line
    assert "ivan" not in line
