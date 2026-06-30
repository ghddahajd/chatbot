"""проверки валидатора клиентской KB."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR / "scripts"))

from validate_kb import validate_kb  # noqa: E402


def test_validate_kb_accepts_universal_sample(tmp_path: Path) -> None:
    source_dir = BACKEND_DIR / "data" / "client_template" / "universal_sample"
    target_dir = tmp_path / "universal_sample"
    shutil.copytree(source_dir, target_dir)

    assert validate_kb(target_dir) == []


def test_validate_kb_rejects_empty_faq(tmp_path: Path) -> None:
    source_dir = BACKEND_DIR / "data" / "client_template" / "universal_sample"
    target_dir = tmp_path / "universal_sample"
    shutil.copytree(source_dir, target_dir)
    (target_dir / "faq.md").write_text("", encoding="utf-8")

    errors = validate_kb(target_dir)

    assert "faq.md: файл пустой" in errors
    assert any("нужно минимум 3 секции" in error for error in errors)


def test_validate_kb_requires_three_faq_sections(tmp_path: Path) -> None:
    source_dir = BACKEND_DIR / "data" / "client_template" / "universal_sample"
    target_dir = tmp_path / "universal_sample"
    shutil.copytree(source_dir, target_dir)
    (target_dir / "faq.md").write_text("# FAQ\n\n## Только одна секция\n\nТекст.\n", encoding="utf-8")

    errors = validate_kb(target_dir)

    assert any("нужно минимум 3 секции" in error for error in errors)
