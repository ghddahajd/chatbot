"""проверка onboarding smoke script."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]


def test_smoke_onboarding_script() -> None:
    result = subprocess.run(
        [sys.executable, str(BACKEND_DIR / "scripts" / "smoke_onboarding.py")],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ИТОГО:" in result.stdout
    assert "❌" not in result.stdout
