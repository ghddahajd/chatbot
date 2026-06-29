"""проверки CLI публикации клиентской KB."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
REQUIRED_FILES = ("company.yaml", "services.json", "prices.json", "faq.md")


def test_onboard_client_publishes_required_files(tmp_path: Path) -> None:
    source_dir = tmp_path / "sample_client"
    clients_dir = tmp_path / "clients"
    shutil.copytree(BACKEND_DIR / "data" / "client_template" / "sample_client", source_dir)

    result = subprocess.run(
        [
            sys.executable,
            str(BACKEND_DIR / "scripts" / "onboard_client.py"),
            str(source_dir),
            "--clients-dir",
            str(clients_dir),
            "--api-base",
            "https://chat.example",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    target_dir = clients_dir / "sample_client"
    assert all((target_dir / file_name).exists() for file_name in REQUIRED_FILES)
    assert (target_dir / "config.yaml").exists()
    assert 'data-company-id="sample_client"' in result.stdout
    assert "Autodetect embed:" in result.stdout


def test_onboard_client_refuses_existing_without_force(tmp_path: Path) -> None:
    source_dir = tmp_path / "sample_client"
    clients_dir = tmp_path / "clients"
    shutil.copytree(BACKEND_DIR / "data" / "client_template" / "sample_client", source_dir)

    base_command = [
        sys.executable,
        str(BACKEND_DIR / "scripts" / "onboard_client.py"),
        str(source_dir),
        "--clients-dir",
        str(clients_dir),
    ]

    first = subprocess.run(base_command, check=False, capture_output=True, text=True)
    second = subprocess.run(base_command, check=False, capture_output=True, text=True)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 1
    assert "уже существует" in second.stderr
