"""проверки widget bootstrap endpoint."""


def test_explicit_company_id_200(test_client) -> None:
    response = test_client.get(
        "/api/widget/bootstrap?company_id=rosh_demo",
        headers={"origin": "http://localhost:5500"},
    )

    assert response.status_code == 200
    assert response.json()["company_id"] == "rosh_demo"
    assert response.json()["features"] == {
        "operator": True,
        "lead_capture": True,
        "analytics": False,
    }


def test_autodetect_by_origin_200(test_client) -> None:
    response = test_client.get(
        "/api/widget/bootstrap",
        headers={"origin": "http://localhost:5500"},
    )

    assert response.status_code == 200
    assert response.json()["company_id"] == "rosh_demo"


def test_unknown_company_id_404(test_client) -> None:
    response = test_client.get(
        "/api/widget/bootstrap?company_id=missing_company",
        headers={"origin": "http://localhost:5500"},
    )

    assert response.status_code == 404


def test_wrong_domain_403(test_client) -> None:
    response = test_client.get(
        "/api/widget/bootstrap?company_id=rosh_demo",
        headers={"origin": "https://wrong.example"},
    )

    assert response.status_code == 403


def test_duplicate_domain_409(test_client) -> None:
    response = test_client.get(
        "/api/widget/bootstrap",
        headers={"origin": "https://duplicate.example"},
    )

    assert response.status_code == 409
