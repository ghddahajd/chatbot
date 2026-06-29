"""проверки CLI публикации клиентской KB."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
REQUIRED_FILES = ("company.yaml", "services.json", "prices.json", "faq.md")


def _remove_allowed_domains(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    result = []
    skip_list = False
    for line in lines:
        if line.startswith("allowed_domains:"):
            skip_list = True
            continue
        if skip_list and line.startswith("  - "):
            continue
        skip_list = False
        result.append(line)
    path.write_text("\n".join(result) + "\n", encoding="utf-8")


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
    assert "widget:" in (target_dir / "config.yaml").read_text(encoding="utf-8")
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


def test_onboard_client_dry_run_does_not_publish(tmp_path: Path) -> None:
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
            "http://localhost:8000",
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not (clients_dir / "sample_client").exists()
    assert "Dry run: KB валидна" in result.stdout
    assert "✅ услуг:" in result.stdout
    assert "Проверка готовности к продаже" in result.stdout
    assert 'data-company-id="sample_client"' in result.stdout
    assert "Для публикации запустите без --dry-run" in result.stdout


def test_onboard_client_dry_run_reports_invalid_kb(tmp_path: Path) -> None:
    source_dir = tmp_path / "broken_client"
    source_dir.mkdir()

    result = subprocess.run(
        [
            sys.executable,
            str(BACKEND_DIR / "scripts" / "onboard_client.py"),
            str(source_dir),
            "--clients-dir",
            str(tmp_path / "clients"),
            "--dry-run",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "KB validation failed" in result.stderr


def test_onboard_client_publish_refuses_blockers(tmp_path: Path) -> None:
    source_dir = tmp_path / "sample_client"
    clients_dir = tmp_path / "clients"
    shutil.copytree(BACKEND_DIR / "data" / "client_template" / "sample_client", source_dir)
    _remove_allowed_domains(source_dir / "company.yaml")

    result = subprocess.run(
        [
            sys.executable,
            str(BACKEND_DIR / "scripts" / "onboard_client.py"),
            str(source_dir),
            "--clients-dir",
            str(clients_dir),
            "--publish",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "БЛОКЕРЫ готовности" in result.stderr
    assert "allowed_domains пустой" in result.stderr
    assert not (clients_dir / "sample_client").exists()
