"""Файловое хранение лидов и чтение их архива."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..utils.jsonl import append_jsonl, read_jsonl


class FileLeadStore:
    def __init__(self, path: Path, archive_path: Optional[Path] = None) -> None:
        self.path = path
        self.archive_path = archive_path

    def append(self, payload: dict[str, Any]) -> None:
        append_jsonl(self.path, payload)

    def read_all(self) -> list[dict[str, Any]]:
        leads = read_jsonl(self.path)
        if self.archive_path is not None:
            leads += read_jsonl(self.archive_path)
        return leads
