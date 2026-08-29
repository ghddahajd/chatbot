"""GET /api/debug/telegram-check — side-effect-free проверка живости Telegram-доставки
(getMe/getChatMember), не путать с /api/debug/trace (полный decision-trace одного сообщения).
См. TelegramBridgeService.health_check для того, почему это отдельный ручной эндпоинт,
не часть автоматически опрашиваемого /health."""


def test_telegram_check_requires_operator_token(test_client) -> None:
    response = test_client.get("/api/debug/telegram-check")

    assert response.status_code == 403


def test_telegram_check_reports_disabled_when_no_bot_token_configured(test_client) -> None:
    # Дефолтный test_client — TELEGRAM_BOT_TOKEN="" в managed_env, реальный
    # TelegramBridgeService всё равно создаётся (см. main.py lifespan), просто .enabled=False.
    response = test_client.get("/api/debug/telegram-check?token=demo-operator-token")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "bot_token": {"status": "skip", "detail": "нет токена/группы — Telegram-бридж отключён"},
        "operators_group": {"status": "skip", "detail": "—"},
    }


def test_telegram_check_delegates_to_bridge_health_check(test_client) -> None:
    class _FakeBridge:
        async def health_check(self):
            return {
                "enabled": True,
                "bot_token": {"status": "ok", "detail": "bot username: @rosh_bot"},
                "operators_group": {"status": "ok", "detail": "доступ есть, status=administrator"},
            }

    test_client.app.state.telegram_bridge_service = _FakeBridge()
    try:
        response = test_client.get("/api/debug/telegram-check?token=demo-operator-token")
    finally:
        # Не оставляем фейк для других тестов, использующих тот же test_client в этом модуле.
        del test_client.app.state.telegram_bridge_service

    assert response.status_code == 200
    assert response.json()["operators_group"]["status"] == "ok"
