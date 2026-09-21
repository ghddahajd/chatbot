"""лёгкие счётчики событий процесса (только в памяти).

Нужны, чтобы тихие сбои (откат LLM на шаблоны, недоступный классификатор) были видны в
preflight, а не только тем, кто читает логи. Хранятся последние вызовы за окно; после
перезапуска процесса счётчики пустые — это нормально, окно короткое.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

WINDOW_SECONDS = 3600
MAX_EVENTS_PER_NAME = 1000


class RuntimeStats:
    def __init__(self, *, window_seconds: int = WINDOW_SECONDS, max_events: int = MAX_EVENTS_PER_NAME) -> None:
        self._window_seconds = window_seconds
        self._max_events = max_events
        self._lock = threading.Lock()
        self._events: dict[str, deque[tuple[float, bool]]] = {}
        self._last: dict[str, dict[str, Any]] = {}

    def record_ok(self, name: str) -> None:
        self._record(name, ok=True, error_type=None)

    def record_error(self, name: str, error: BaseException | str | None = None) -> None:
        error_type = error if isinstance(error, str) else type(error).__name__ if error is not None else None
        self._record(name, ok=False, error_type=error_type)

    def _record(self, name: str, *, ok: bool, error_type: str | None) -> None:
        now = time.time()
        with self._lock:
            self._events.setdefault(name, deque(maxlen=self._max_events)).append((now, ok))
            last = self._last.setdefault(name, {"last_ok_at": None, "last_error_at": None, "last_error_type": None})
            if ok:
                last["last_ok_at"] = now
            else:
                last["last_error_at"] = now
                last["last_error_type"] = error_type

    def summary(self, prefix: str = "") -> dict[str, dict[str, Any]]:
        """по каждому имени: сколько успехов и ошибок за окно, когда были последние."""

        cutoff = time.time() - self._window_seconds
        result: dict[str, dict[str, Any]] = {}
        with self._lock:
            for name, events in self._events.items():
                if not name.startswith(prefix):
                    continue
                recent = [ok for moment, ok in events if moment >= cutoff]
                result[name] = {
                    "ok": sum(1 for ok in recent if ok),
                    "errors": sum(1 for ok in recent if not ok),
                    **self._last[name],
                }
        return result

    def reset(self) -> None:
        with self._lock:
            self._events.clear()
            self._last.clear()


# один экземпляр на процесс: вызывающему коду не нужно таскать его через request.app.state
STATS = RuntimeStats()
