"""Подключение клиник без значений и содержимого соседнего клиента."""

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from app.analytics_panel import render_analytics_panel
from app.hours import is_currently_open


@pytest.fixture()
def published_clinics(managed_env):
    backend = Path(__file__).resolve().parents[1]
    clinics = [("clinic_alpha", "Казань", "Europe/Moscow", 1700),
               ("clinic_beta", "Новосибирск", "Asia/Novosibirsk", 6900)]
    for cid, city, tz, price in clinics:
        source = managed_env["temp_dir"] / "staging" / cid
        shutil.copytree(backend / "data/client_template/medical_sample", source)
        company = yaml.safe_load((source / "company.yaml").read_text())
        company.update(company_id=cid, company_name=cid, city=city, timezone=tz,
                       phone=f"Телефон {cid}", address=f"Адрес {cid}",
                       allowed_domains=[f"{cid}.example"], website_url=f"https://{cid}.example",
                       telegram_url=f"https://t.me/{cid}",
                       working_hours_schedule={"mon": {"open": "09:00", "close": "18:00"}})
        (source / "company.yaml").write_text(yaml.safe_dump(company, allow_unicode=True))
        config = yaml.safe_load((source / "config.yaml").read_text())
        config["widget"].update(header_title=cid, primary_color="#123456" if price == 1700 else "#654321")
        config["phrasebook"]["greeting"] = f"Добро пожаловать в {cid}"
        (source / "config.yaml").write_text(yaml.safe_dump(config, allow_unicode=True))
        services = json.loads((source / "services.json").read_text())
        services[0]["price_from"] = price
        (source / "services.json").write_text(json.dumps(services, ensure_ascii=False))
        prices = json.loads((source / "prices.json").read_text())
        prices[0]["price_text"] = f"{price:,} ₽".replace(",", " ")
        (source / "prices.json").write_text(json.dumps(prices, ensure_ascii=False))
        (source / "faq_quick_answers.json").write_text(json.dumps([
            {"id": "parking", "question": "Есть парковка?", "answer": f"Парковка {cid}"}]))
        (source / "article_service_map.yaml").write_text(yaml.safe_dump({"items": [{
            "url": f"https://{cid}.example/service", "title": "Приём",
            "status": "approved", "service_ids": ["primary_consultation"], "teaser_title": cid}]}))
        (source / "symptom_service_map.yaml").write_text(yaml.safe_dump({cid: ["primary_consultation"]}))
        result = subprocess.run([sys.executable, str(backend / "scripts/onboard_client.py"),
                                 str(source), "--clients-dir", str(managed_env["clients_dir"])],
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    return clinics


def test_published_clients_keep_own_content_and_hours(published_clinics, test_client):
    resolver = test_client.app.state.knowledge_base_resolver
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    previous_session = None
    for cid, city, tz, price in published_clinics:
        response = test_client.get(f"/api/widget/bootstrap?company_id={cid}&page=/service",
                                   headers={"Origin": f"https://{cid}.example"})
        assert response.status_code == 200
        data = response.json()
        assert data["company_name"] == cid
        assert data["city"] == city
        assert data["website_url"] == f"https://{cid}.example"
        assert data["telegram_url"] == f"https://t.me/{cid}"
        assert data["widget_config"]["header_title"] == cid
        assert data["widget_config"]["primary_color"] == ("#123456" if price == 1700 else "#654321")
        assert data["greeting"] == f"Добро пожаловать в {cid}"
        assert data["quick_faq"][0]["answer"] == f"Парковка {cid}"
        assert data["teaser_title"] == cid
        other_cid = "clinic_beta" if cid == "clinic_alpha" else "clinic_alpha"
        assert test_client.get(f"/api/widget/bootstrap?company_id={cid}",
                              headers={"Origin": f"https://{other_cid}.example"}).status_code == 403
        kb = resolver.get(cid, fallback=False)
        assert kb.symptom_service_map == {cid.replace("_", " "): ["primary_consultation"]}
        assert kb.rag_corpus_path() is None
        assert kb.company.timezone == tz
        assert is_currently_open(kb.company.working_hours_schedule, tz, now=now) == (price == 1700)
        settings = test_client.get(f"/api/settings/company?company_id={cid}",
                                   headers={"X-Operator-Token": "demo-operator-token"}).json()
        assert settings["phone"] == f"Телефон {cid}"
        assert settings["address"] == f"Адрес {cid}"
        chat = test_client.post("/api/chat/message", json={"company_id": cid, "session_id": previous_session,
                                "message": "Сколько стоит первичная консультация?"}).json()
        assert f"{price:,}".replace(",", " ") in chat["answer"]
        assert "РОШ" not in chat["answer"]
        assert chat["session_id"] != previous_session
        previous_session = chat["session_id"]


def test_panel_uses_registered_clients_and_valid_default(test_client):
    response = test_client.get("/backstage?token=demo-operator-token")
    assert response.status_code == 200
    for company_id in ("rosh_demo", "dup_one", "dup_two"):
        assert response.text.count(f'<option value="{company_id}"') == 1
    assert 'value="rosh_demo" selected' in response.text
    assert "rosh_import_demo" not in response.text
    assert "rosh_test" not in response.text


def test_panel_without_rosh_defaults():
    panel = render_analytics_panel(default_company_id="clinic_b")
    assert '<option value="clinic_b" selected>' in panel
    assert "rosh_import_demo" not in panel
    assert "rosh_test" not in panel


def test_panel_keeps_configured_registered_default():
    panel = render_analytics_panel(default_company_id="rosh_test",
                                   known_company_ids=["clinic_b", "rosh_test", "rosh_test"])
    assert panel.count('<option value="rosh_test"') == 1
    assert 'value="rosh_test" selected' in panel


def test_resolver_does_not_list_incomplete_or_external_clients(resolver, managed_env, tmp_path):
    root = managed_env["clients_dir"]
    (root / "incomplete").mkdir()
    outside = tmp_path / "outside"
    shutil.copytree(root / "dup_one", outside)
    (root / "external").symlink_to(outside, target_is_directory=True)
    assert resolver.company_ids() == ["dup_one", "dup_two", "rosh_demo"]


def test_login_is_not_branded_as_rosh(test_client):
    response = test_client.get("/login")
    assert "Аналитика ROSH" not in response.text
