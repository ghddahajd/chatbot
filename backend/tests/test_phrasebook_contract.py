"""контракт phrasebook: любой читаемый ключ должен иметь дефолт."""

from __future__ import annotations

import re
from pathlib import Path

from app.knowledge import DEFAULT_PHRASEBOOK, KnowledgeBaseResolver


APP_DIR = Path(__file__).resolve().parents[1] / "app"
PHRASE_KEY_PATTERN = re.compile(
    r"(?:self\.)?_phrase\(\s*(?:knowledge_base,\s*)?[\"']([a-zA-Z0-9_]+)[\"']"
)


def _read_phrase_keys() -> set[str]:
    keys: set[str] = set()
    for path in APP_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        keys.update(PHRASE_KEY_PATTERN.findall(text))
    return keys


def test_phrasebook_covers_all_read_keys() -> None:
    read_keys = _read_phrase_keys()

    assert read_keys
    assert read_keys - set(DEFAULT_PHRASEBOOK) == set()


def test_phrasebook_client_override_applies(managed_env: dict[str, Path]) -> None:
    config_path = managed_env["clients_dir"] / "rosh_demo" / "config.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8")
        + "\n"
        + "phrasebook:\n"
        + '  contact_cancelled: "CUSTOM CONTACT CANCELLED"\n'
        + '  operator_soft_offer: "CUSTOM OPERATOR OFFER"\n',
        encoding="utf-8",
    )

    resolver = KnowledgeBaseResolver(
        data_dir=Path(__file__).resolve().parents[1] / "data",
        clients_data_dir=managed_env["clients_dir"],
        defaults_data_dir=Path(__file__).resolve().parents[1] / "data" / "defaults",
        default_company_id="rosh_demo",
    )

    phrasebook = resolver.phrasebook("rosh_demo")

    assert phrasebook["contact_cancelled"] == "CUSTOM CONTACT CANCELLED"
    assert phrasebook["operator_soft_offer"] == "CUSTOM OPERATOR OFFER"
