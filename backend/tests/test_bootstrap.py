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
        "voice_input": True,
    }
    assert response.json()["widget_config"]["primary_color"] == "#1F7A5C"
    assert response.json()["widget_config"]["position"] == "bottom-right"


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


def test_widget_config_from_client_config(test_client, managed_env) -> None:
    config_path = managed_env["clients_dir"] / "rosh_demo" / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "features:",
                "  operator: true",
                "  lead_capture: true",
                "  analytics: false",
                "  voice_input: false",
                "widget:",
                '  primary_color: "#B85C38"',
                '  button_color: "#7A1F1F"',
                '  header_title: "Клиника РОШ"',
                '  header_subtitle: "Запись и цены"',
                '  position: "bottom-left"',
                '  avatar_emoji: "👩‍⚕️"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    response = test_client.get(
        "/api/widget/bootstrap?company_id=rosh_demo",
        headers={"origin": "http://localhost:5500"},
    )

    assert response.status_code == 200
    assert response.json()["features"]["voice_input"] is False
    assert response.json()["widget_config"] == {
        "primary_color": "#B85C38",
        "button_color": "#7A1F1F",
        "header_title": "Клиника РОШ",
        "header_subtitle": "Запись и цены",
        "position": "bottom-left",
        "avatar_emoji": "👩‍⚕️",
    }
