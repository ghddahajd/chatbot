"""общие проверки доступа для админских роутов."""

from typing import Optional

from fastapi import HTTPException, Request


OPERATOR_COOKIE_NAME = "operator_token"


def verify_operator_token(request: Request, header_token: Optional[str]) -> None:
    query_token = request.query_params.get("token")
    # Кука (2026-08-27, /login) — тот же токен, просто больше не обязан жить в URL, где он
    # утекает в историю браузера/логи сервера/случайный скриншот. Header/query остаются
    # рабочими ради обратной совместимости со старыми ссылками и прямыми API-вызовами.
    cookie_token = request.cookies.get(OPERATOR_COOKIE_NAME)
    expected = request.app.state.settings.operator_token
    provided = header_token or query_token or cookie_token
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid operator token")
