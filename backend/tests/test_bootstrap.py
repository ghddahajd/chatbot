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
    assert response.json()["greeting"]


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


def test_bootstrap_greeting_uses_client_phrasebook_variant(test_client, managed_env) -> None:
    config_path = managed_env["clients_dir"] / "rosh_demo" / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                "phrasebook:",
                "  greeting:",
                '    - "Привет из тестового приветствия!"',
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
    assert response.json()["greeting"] == "Привет из тестового приветствия!"


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


def test_settings_widget_override_reaches_real_widget_bootstrap(test_client) -> None:
    """Живой баг (найден при пере-проверке "Настройки"-таба, 2026-08-29): widget_config()
    читает config.yaml заново с диска при каждом вызове, в обход кэша, где лежит уже
    смерженный с оверрайдом config_payload — без явного мержа оверрайда прямо в
    widget_config() смена брендинга через "Настройки" была бы видна только в самой
    админке (POST/GET /api/settings/company), но никогда не долетала бы до настоящего
    виджета на сайте клиента."""

    save_response = test_client.post(
        "/api/settings/company?company_id=rosh_demo",
        json={
            "phone": "+7 000 000-00-00",
            "address": None,
            "telegram_url": None,
            "website_url": None,
            "working_hours_schedule": {day: None for day in
                                       ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
            "widget": {
                "primary_color": "#111111",
                "button_color": "#222222",
                "header_title": "Тестовый заголовок",
                "header_subtitle": "Тестовый подзаголовок",
                "position": "bottom-left",
                "avatar_emoji": "🧪",
            },
            "facts": {
                "oms": False, "dms": False, "ambulance_brings": False,
                "sells_products": False, "discloses_doctor_schedule": False,
            },
        },
        headers={"x-operator-token": "demo-operator-token"},
    )
    assert save_response.status_code == 200

    bootstrap_response = test_client.get(
        "/api/widget/bootstrap?company_id=rosh_demo",
        headers={"origin": "http://localhost:5500"},
    )

    assert bootstrap_response.status_code == 200
    widget_config = bootstrap_response.json()["widget_config"]
    assert widget_config["header_title"] == "Тестовый заголовок"
    assert widget_config["primary_color"] == "#111111"
    assert widget_config["position"] == "bottom-left"
