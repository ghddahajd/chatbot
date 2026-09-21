"""backend/scripts/preflight.py — запуск preflight одной командой: поиск токена, отчёт, код возврата, пробы."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

import preflight as script  # noqa: E402


def test_read_env_file_handles_comments_quotes_and_missing_key(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# комментарий\nOTHER=1\nOPERATOR_TOKEN='abc-123'\nEMPTY=\n",
        encoding="utf-8",
    )

    assert script._read_env_file(env_file, "OPERATOR_TOKEN") == "abc-123"
    assert script._read_env_file(env_file, "EMPTY") is None
    assert script._read_env_file(env_file, "MISSING") is None
    assert script._read_env_file(tmp_path / "nope.env", "OPERATOR_TOKEN") is None


def test_find_token_prefers_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPERATOR_TOKEN", "from-env")

    assert script.find_token("OPERATOR_TOKEN", "some-container") == "from-env"


@pytest.mark.parametrize(
    ("report_status", "probe_statuses", "expected"),
    [
        ("ok", None, 0),
        ("degraded", None, 1),
        ("error", None, 2),
        ("ok", ["ok", "degraded"], 1),
        ("ok", ["ok", "error"], 2),
        ("skip", None, 0),
    ],
)
def test_exit_code_reflects_worst_status(report_status, probe_statuses, expected) -> None:
    probes = [{"status": status} for status in probe_statuses] if probe_statuses else None

    assert script.overall_exit_code({"status": report_status}, probes) == expected


def test_render_prints_real_report_without_secrets(test_client) -> None:
    report = test_client.get("/api/debug/preflight", headers={"X-Operator-Token": "demo-operator-token"}).json()

    text = script.render(report, None, "http://127.0.0.1:8000")

    assert "Итог:" in text
    assert "rosh_demo" in text
    assert "leads" in text
    assert "demo-operator-token" in text  # значение по умолчанию называется в детали прямо, это не секрет
    assert "Красные линии" not in text


def _fake_trace(reason: str, action: str = "answer") -> dict:
    return {"final_action": action, "steps": [{"step": "policy_decision", "result": {"reason": reason}}]}


def test_probes_mark_critical_mismatch_as_error_and_others_as_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_request(method, url, token, *, body=None, timeout):
        message = body["message"]
        if "жить" in message:  # критичная проба: кризис не распознан
            return _fake_trace("ok")
        if "стоит" in message:  # некритичная: цена ушла в другую причину
            return _fake_trace("unknown_service")
        expected = {
            "болит живот": "regulated_advice",
            "оператора": "operator_requested",
            "запишите": "booking_request",
            "адрес": "ok",
            "хламидиоз": "regulated_advice",
        }
        reason = next(value for key, value in expected.items() if key in message)
        return _fake_trace(reason)

    monkeypatch.setattr(script, "_request", fake_request)

    results = {probe["name"]: probe for probe in script.run_probes("http://x", "t", "rosh_import_demo", 5)}

    assert results["кризис и самоповреждение"]["status"] == "error"
    assert results["цена услуги"]["status"] == "degraded"
    assert results["медицинский вопрос"]["status"] == "ok"
    assert results["контакты"]["status"] == "ok"
    assert len(results) == 7


def test_probe_request_failure_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(script, "_request", broken)

    results = script.run_probes("http://x", "t", "rosh_import_demo", 5)

    assert all(probe["status"] == "error" for probe in results)


def test_save_report_writes_timestamped_file_into_directory(tmp_path: Path) -> None:
    saved = script.save_report(tmp_path / "reports", {"report": {"status": "ok"}})

    assert saved.parent == tmp_path / "reports"
    assert saved.name.startswith("preflight_") and saved.suffix == ".json"
    assert '"status": "ok"' in saved.read_text(encoding="utf-8")


def test_exit_code_counts_host_and_external_groups() -> None:
    report = {"status": "ok"}

    assert script.overall_exit_code(report, None, {"status": "ok"}, [{"status": "ok"}]) == 0
    assert script.overall_exit_code(report, None, {"status": "degraded"}, None) == 1
    assert script.overall_exit_code(report, None, None, [{"status": "ok"}, {"status": "error"}]) == 2


# ---------------------------------------------------------------- отпечаток кода на диске


def test_script_fingerprint_matches_app_fingerprint() -> None:
    """скрипт на хосте и приложение в контейнере обязаны считать отпечаток одинаково."""

    from app.preflight import code_fingerprint

    assert script.code_fingerprint(BACKEND_DIR / "app") == code_fingerprint()


def _app_report(fingerprint: str) -> dict:
    return {"checks": {"app": {"code_fingerprint": fingerprint}}}


def test_local_code_check_detects_pull_without_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(script, "code_fingerprint", lambda root: "aaaaaaaaaaaa")

    same = script.local_code_check(_app_report("aaaaaaaaaaaa"), "http://127.0.0.1:8000")
    different = script.local_code_check(_app_report("bbbbbbbbbbbb"), "http://127.0.0.1:8000")

    assert same["status"] == "ok"
    assert different["status"] == "degraded"
    assert "без docker compose up" in different["detail"]


def test_local_code_check_is_skipped_for_remote_url_or_old_server() -> None:
    assert script.local_code_check(_app_report("aaaaaaaaaaaa"), "https://roshbot.ru") is None
    assert script.local_code_check({"checks": {"app": {}}}, "http://127.0.0.1:8000") is None


# ---------------------------------------------------------------- взгляд снаружи


def test_parse_and_extract_domain_targets() -> None:
    assert script.parse_domain_targets("a:x.ru, b:y.ru,broken") == [("a", "x.ru"), ("b", "y.ru")]
    report = {"checks": {"domains": {"domains": [{"domain": "x.ru", "companies": ["a"]}]}}}
    assert script.targets_from_report(report) == [("a", "x.ru")]
    assert script.targets_from_report({"checks": {}}) == []


@pytest.fixture()
def fake_site():
    """настоящий HTTP-сервер на localhost: отдаёт /health, widget.js и bootstrap по заданным маршрутам."""

    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    routes: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            status, headers, body = routes.get(path, (404, {}, b"{}"))
            if callable(status):
                status, headers, body = status(self.headers.get("Origin"))
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", routes
    finally:
        server.shutdown()
        server.server_close()


def _healthy_routes(routes: dict, *, cors: bool = True, bootstrap_status: int = 200) -> None:
    js = ("/* widget */" + " " * 2000).encode("utf-8")
    routes["/health"] = (200, {"Content-Type": "application/json"}, b'{"status": "ok"}')
    routes["/static/widget.js"] = (200, {"Content-Type": "application/javascript"}, js)

    def bootstrap(origin):
        headers = {"Content-Type": "application/json"}
        if cors and bootstrap_status == 200:
            headers["Access-Control-Allow-Origin"] = origin
        return bootstrap_status, headers, b'{"company_id": "c1"}'

    routes["/api/widget/bootstrap"] = (bootstrap, {}, b"")


def test_external_checks_all_green(fake_site) -> None:
    base, routes = fake_site
    _healthy_routes(routes)

    items = {item["name"]: item for item in script.external_checks(base, [("c1", "client.ru")], 5)}

    assert items["/health снаружи"]["status"] == "ok"
    assert items["widget.js"]["status"] == "ok"
    assert items["виджет на client.ru (c1)"]["status"] == "ok"


def test_external_checks_flags_missing_cors_and_forbidden_domain(fake_site) -> None:
    base, routes = fake_site
    _healthy_routes(routes, cors=False)
    no_cors = script.external_checks(base, [("c1", "client.ru")], 5)[-1]

    _healthy_routes(routes, bootstrap_status=403)
    forbidden = script.external_checks(base, [("c1", "client.ru")], 5)[-1]

    assert no_cors["status"] == "error" and "CORS" in no_cors["detail"]
    assert forbidden["status"] == "error" and "allowed_domains" in forbidden["detail"]


def test_external_checks_flags_degraded_health_and_broken_widget(fake_site) -> None:
    base, routes = fake_site
    routes["/health"] = (207, {"Content-Type": "application/json"}, b'{"status": "degraded"}')
    routes["/static/widget.js"] = (200, {"Content-Type": "text/html"}, b"<html>oops</html>")

    items = {item["name"]: item for item in script.external_checks(base, [], 5)}

    assert items["/health снаружи"]["status"] == "degraded"
    assert items["widget.js"]["status"] == "error"


def test_external_checks_reports_unreachable_site() -> None:
    items = script.external_checks("http://127.0.0.1:9", [("c1", "client.ru")], 2)

    assert items[0]["status"] == "error"
    assert all(item["status"] == "error" for item in items)


@pytest.mark.parametrize(("days", "expected"), [(90, "ok"), (15, "degraded"), (5, "error")])
def test_tls_certificate_thresholds(monkeypatch: pytest.MonkeyPatch, days, expected) -> None:
    monkeypatch.setattr(script, "tls_days_left", lambda host, port, timeout: days)
    monkeypatch.setattr(script, "_http_get", lambda url, **kwargs: (200, {"content-type": "application/javascript"}, b"x" * 2000))

    items = {item["name"]: item for item in script.external_checks("https://example.test", [], 5)}

    assert items["TLS-сертификат"]["status"] == expected
