"""общие проверки доступа для админских роутов."""

from typing import Optional

from fastapi import HTTPException, Request


def verify_operator_token(request: Request, header_token: Optional[str]) -> None:
    query_token = request.query_params.get("token")
    expected = request.app.state.settings.operator_token
    provided = header_token or query_token
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid operator token")
