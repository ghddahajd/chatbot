"""Контекст выбранной клиники без раскрытия данных соседнего клиента."""

import json
import re
import shutil
import subprocess

import pytest

from app.analytics_panel import render_analytics_panel
from .test_client_access import staff_client


def contexts(html):
    return json.loads(re.search(r"const CLINIC_CONTEXTS = (.*);", html).group(1))


def test_owner_page_includes_registered_clinic_contexts(test_client):
    data = contexts(test_client.get("/backstage?token=demo-operator-token").text)
    assert set(data) == {"rosh_demo", "dup_one", "dup_two"}
    assert data["dup_one"]["name"] == "dup_one"
    assert data["dup_one"]["domain"] == "duplicate.example"


def test_staff_page_only_includes_own_context(staff_client):
    data = contexts(staff_client.get("/analytics").text)
    assert set(data) == {"dup_one"}


def test_primary_domain_and_tooltip_do_not_contain_url_credentials():
    html = render_analytics_panel(default_company_id="clinic", company_contexts={"clinic": {
        "company_name": "Клиника", "website_url": "https://sample:private@example.org/path?token=private",
        "allowed_domains": ["example.org", "second.example"]}})
    data = contexts(html)["clinic"]
    assert data == {"name": "Клиника", "domain": "example.org", "domains": ["example.org", "second.example"]}
    assert "private" not in html


def test_missing_or_invalid_website_uses_allowed_domain():
    html = render_analytics_panel(default_company_id="clinic", company_contexts={"clinic": {
        "company_name": "Клиника", "website_url": "https://[broken", "allowed_domains": ["clinic.example"]}})
    assert contexts(html)["clinic"]["domain"] == "clinic.example"
    assert contexts(render_analytics_panel(default_company_id="clinic"))["clinic"]["domain"] == "Домен не указан"


def test_test_client_does_not_display_copied_production_website():
    page = render_analytics_panel(default_company_id="test", company_contexts={"test": {
        "company_name": "Тест", "website_url": "https://production.example",
        "allowed_domains": ["localhost", "preview.example"]}})
    assert contexts(page)["test"]["domain"] == "preview.example"
    assert "production.example" not in page


def test_context_is_safe_inside_script_and_preserves_literal_placeholders():
    name = '</script><script>alert(1)</script> __CHAT_EXPORT_HTML__'
    html = render_analytics_panel(default_company_id="clinic", company_contexts={"clinic": {"company_name": name}})
    assert contexts(html)["clinic"]["name"] == name
    assert "</script><script>" not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="Для проверки встроенного JS нужен Node.js")
def test_embedded_script_and_context_switching(tmp_path):
    page = render_analytics_panel(default_company_id="a", known_company_ids=["a", "b"], company_contexts={
        "a": {"company_name": "Альфа", "website_url": "https://alpha.example"},
        "b": {"company_name": "Бета", "allowed_domains": ["beta.example", "other.example"]}})
    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    path = tmp_path / "panel.js"
    path.write_text(script, encoding="utf-8")
    syntax = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
    assert syntax.returncode == 0, syntax.stderr
    function = re.search(r"function updateClinicContext\(\) \{.*?\n    \}", script, re.S).group(0)
    path.write_text('const assert=require("node:assert/strict"); const CLINIC_CONTEXTS=' + json.dumps(contexts(page)) + ';\n'
        'const elements={companySelect:{value:"a"},clinicContextName:{},clinicContextDomain:{},clinicContext:{}};\n'
        'const document={getElementById:id=>elements[id]};\n' + function + '\n'
        'updateClinicContext(); assert.equal(elements.clinicContextName.textContent,"Альфа");\n'
        'elements.companySelect.value="b"; updateClinicContext(); assert.equal(elements.clinicContextDomain.textContent,"beta.example");\n'
        'assert.equal(elements.clinicContext.title,"beta.example\\nother.example");\n'
        'elements.companySelect.value=""; updateClinicContext(); assert.equal(elements.clinicContextName.textContent,"Все клиники");\n'
        'assert.equal(elements.clinicContextDomain.textContent,""); assert.equal(elements.clinicContext.title,"");\n', encoding="utf-8")
    result = subprocess.run(["node", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
