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


def client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip() or "unknown"
    if request.client is None:
        return "unknown"
    return request.client.host
