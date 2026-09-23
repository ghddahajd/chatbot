"""путь страницы сайта клиента для аналитики."""

from __future__ import annotations

from urllib.parse import unquote, urlsplit


def normalize_page(raw: str) -> str:
    """только путь страницы: без домена, «?…» и «#…» — там бывают рекламные метки и личные данные."""

    if not raw.strip():
        return ""
    path = unquote(urlsplit(raw.strip()).path)[:200]
    return path.rstrip("/") or "/"
