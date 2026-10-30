"""проверяем состояние уведомления настоящими методами виджета, без новых JS-зависимостей."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_operator_reply_notice() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("для проверки логики виджета нужен Node.js")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, "--test", "widget/tests/operator_notice.test.cjs"],
        cwd=root, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
