"""каждый скрипт с argparse должен хотя бы запускаться: `--help` ловит сломанный импорт или
опечатку в верхнеуровневом коде до того, как это найдёт живой человек на сервере.

Не проверяет логику скрипта — только то, что модуль вообще импортируется и парсер строится.
Роли скриптов описаны в backend/scripts/README.md."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
# без argparse: интерактивная консоль (kb_console) и smoke-тесты, которые сами являются
# pytest-подобным набором без CLI (smoke_managed, smoke_onboarding, demo_gate — тонкая
# обёртка над vendor_compare, у неё есть свой argparse через делегирование).
NO_ARGPARSE_SCRIPTS = {"kb_console.py", "smoke_managed.py", "smoke_onboarding.py"}


def _argparse_scripts() -> list[Path]:
    return sorted(
        path
        for path in SCRIPTS_DIR.glob("*.py")
        if path.name not in NO_ARGPARSE_SCRIPTS and "import argparse" in path.read_text(encoding="utf-8")
    )


@pytest.mark.parametrize("script", _argparse_scripts(), ids=lambda path: path.name)
def test_script_help_runs(script: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def test_readme_documents_every_script() -> None:
    """не даёт добавить новый скрипт без строки в README.md с его ролью."""

    readme = (SCRIPTS_DIR / "README.md").read_text(encoding="utf-8")
    missing = [
        path.name
        for path in SCRIPTS_DIR.glob("*.py")
        if path.name != "__init__.py" and f"`{path.name}`" not in readme
    ]

    assert not missing, f"нет в README.md: {missing}"


def test_no_argparse_scripts_still_exist_and_stay_out_of_the_help_check() -> None:
    """если у одного из них появится argparse, список выше устареет — тест должен упасть."""

    for name in NO_ARGPARSE_SCRIPTS:
        path = SCRIPTS_DIR / name
        assert path.exists(), f"{name} больше не существует — почисти NO_ARGPARSE_SCRIPTS"
        assert "import argparse" not in path.read_text(encoding="utf-8"), (
            f"{name} теперь использует argparse — убери его из NO_ARGPARSE_SCRIPTS"
        )
