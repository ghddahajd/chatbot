"""общие проверки доступа для админских роутов."""

import hmac
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from fastapi import HTTPException, Request


OPERATOR_COOKIE_NAME = "operator_token"
CLIENT_COOKIE_NAME = "client_session"


@dataclass(frozen=True)
class Principal:
    owner: bool
    company_id: Optional[str] = None
    account_id: Optional[str] = None


def require_same_origin(request: Request) -> None:
    source = request.headers.get("origin") or request.headers.get("referer")
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    expected = (scheme, request.url.netloc)
    try:
        parsed = urlsplit(source or "")
        matches = (parsed.scheme, parsed.netloc) == expected and not parsed.username
    except ValueError:
        matches = False
    if not matches:
        raise HTTPException(status_code=403, detail="Запрос должен исходить с этой страницы")


def is_owner(request: Request, header_token: Optional[str] = None) -> bool:
    provided = header_token or request.query_params.get("token") or request.cookies.get(OPERATOR_COOKIE_NAME)
    expected = request.app.state.settings.operator_token
    return bool(expected and provided and hmac.compare_digest(provided.encode(), expected.encode()))


def verify_operator_token(request: Request, header_token: Optional[str]) -> None:
    if not is_owner(request, header_token):
        raise HTTPException(status_code=403, detail="Invalid operator token")
    if (request.app.state.settings.client_login_enabled and request.method not in {"GET", "HEAD", "OPTIONS"}
            and not header_token and not request.query_params.get("token")):
        require_same_origin(request)


async def authenticate(request: Request, header_token: Optional[str] = None) -> Principal:
    if is_owner(request, header_token):
        verify_operator_token(request, header_token)
        return Principal(owner=True)
    if not request.app.state.settings.client_login_enabled:
        raise HTTPException(status_code=403, detail="Требуется вход")
    token = request.cookies.get(CLIENT_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=403, detail="Требуется вход")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        require_same_origin(request)
    service = getattr(request.app.state, "client_access", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Вход сотрудников временно недоступен")
    try:
        principal = await service.authenticate(token)
    except Exception:
        raise HTTPException(status_code=503, detail="Вход сотрудников временно недоступен") from None
    if principal is None:
        raise HTTPException(status_code=403, detail="Сессия входа недействительна")
    return principal


async def authorize_company(request: Request, header_token: Optional[str], company_id: Optional[str]):
    principal = await authenticate(request, header_token)
    if principal.owner:
        return company_id
    if company_id and company_id != principal.company_id:
        raise HTTPException(status_code=403, detail="Нет доступа к этой клинике")
    return principal.company_id
