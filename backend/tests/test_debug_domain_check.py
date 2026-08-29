"""GET /api/debug/domain-check — side-effect-free сверка "каждый настроенный домен
резолвится ровно в одного клиента". Переиспользует KnowledgeBaseResolver.build_domain_index()
(тот же индекс, что реальный автодетект по Origin в /api/widget/bootstrap)."""


def test_domain_check_requires_operator_token(test_client) -> None:
    response = test_client.get("/api/debug/domain-check")
    assert response.status_code == 403


def test_domain_check_reports_ok_for_unique_domains(test_client) -> None:
    response = test_client.get("/api/debug/domain-check?token=demo-operator-token")
    assert response.status_code == 200
    domains_by_name = {item["domain"]: item for item in response.json()["domains"]}

    assert domains_by_name["medcenterrosh.ru"] == {
        "domain": "medcenterrosh.ru",
        "status": "ok",
        "company_id": "rosh_demo",
    }
    assert domains_by_name["www.medcenterrosh.ru"]["status"] == "ok"


def test_domain_check_excludes_localhost(test_client) -> None:
    # localhost сознательно исключён из отчёта — им делится куча локальных тестовых
    # клиентов, это давно известный факт, не то, что стоит показывать как проблему.
    response = test_client.get("/api/debug/domain-check?token=demo-operator-token")
    domains = {item["domain"] for item in response.json()["domains"]}
    assert "localhost" not in domains


def test_domain_check_flags_duplicate_domain(test_client) -> None:
    # dup_one/dup_two — существующая фикстура conftest.py, оба объявляют один и тот же
    # allowed_domains: ["duplicate.example"] специально для этого сценария.
    response = test_client.get("/api/debug/domain-check?token=demo-operator-token")
    domains_by_name = {item["domain"]: item for item in response.json()["domains"]}

    entry = domains_by_name["duplicate.example"]
    assert entry["status"] == "error"
    assert "dup_one" in entry["detail"]
    assert "dup_two" in entry["detail"]
