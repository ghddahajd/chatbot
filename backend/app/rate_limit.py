"""простые in-memory rate limits для MVP."""

from __future__ import annotations

from collections import defaultdict, deque
from time import monotonic
from typing import Deque

from fastapi import Request


class RateLimiter:
    """скользящее окно запросов на один ключ."""

    def __init__(self, *, limit: int, window_seconds: int = 60) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = monotonic()
        cutoff = now - self.window_seconds
        hits = self._hits[key]
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


def client_ip(request: Request, *, trusted_proxy_count: int = 1) -> str:
    """IP клиента с учётом доверенных обратных прокси.

    X-Forwarded-For — это цепочка, где каждый прокси ДОПИСЫВАЕТ в конец IP того, кто к
    нему подключился. Клиент управляет только тем, что сам вписал в начало строки; всё,
    что дописали наши proxy (справа), подделать нельзя. Раньше брался первый (самый левый,
    полностью клиентский) элемент — это давало обойти rate-limit подменой заголовка на
    каждый запрос. trusted_proxy_count — сколько таких доверенных хопов стоит перед нами.
    """

    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for and trusted_proxy_count > 0:
        parts = [part.strip() for part in forwarded_for.split(",") if part.strip()]
        if len(parts) >= trusted_proxy_count:
            return parts[-trusted_proxy_count]
        # Меньше хопов, чем ожидали доверенных прокси — не доверяем клиентской части
        # заголовка в этой аномалии, падаем на прямой TCP-пир (см. ниже), а не на parts[0].
    if request.client is None:
        return "unknown"
    return request.client.host
