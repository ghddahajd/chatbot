"""Файловая история состояний generic outbox."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..utils.jsonl import append_jsonl, read_jsonl


class FileOutboxStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    async def append(self, record: dict[str, Any]) -> None:
        async with self._lock:
            append_jsonl(self.path, record)

    def latest(self) -> dict[str, dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for record in read_jsonl(self.path):
            delivery_id = str(record.get("delivery_id") or "")
            if delivery_id:
                latest[delivery_id] = record
        return latest
