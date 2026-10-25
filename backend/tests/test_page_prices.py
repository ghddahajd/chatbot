"""цена под страницу: на «сколько стоит?» бот смотрит, с какой страницы пишут, и называет цену её услуги."""

from __future__ import annotations

import yaml

from app import policy
from app.models import PolicyReason, Session

from .test_policy import _copy_rosh_import_kb

SITE = "https://www.medcenterrosh.ru"
PAGES = [
    {"url": f"{SITE}/services/biorevitalizaciya/", "service_ids": ["biorevitalizaciya_9d426f68"]},
    {
        "url": f"{SITE}/blog/sperma-lososya-pdrn",
        "service_ids": ["biorevitalizaciya_9d426f68"],
        "price_focus": ["plinest", "nucleo"],
        "price_focus_title": "PDRN-препараты",
    },
    {
        "url": f"{SITE}/blog/kokki-v-mazke-u-jenschin-chto-eto-znachit-i-chem-opasny",
        "service_ids": ["ginekologiya_fd7d28b2"],
        "price_focus": ["консультация гинеколога"],
        "price_focus_title": "Консультация гинеколога",
    },
    {
        "url": f"{SITE}/problems/starenie-koji-ruk",
        "service_ids": ["fillery_f2df3e74", "fotolechenie_bbl_85e80491", "mezoterapiya_a62ea8d4", "pilingi_8dde1279"],
    },
]
OVERVIEW = "Какие услуги у вас есть и сколько стоят?"
LIST_SERVICES = {"intent": "list_services", "confidence": 0.9}
PRICE_NO_SERVICE = {"intent": "price_question", "service_id": None, "confidence": 0.86}


def _kb(resolver, managed_env):
    kb = _copy_rosh_import_kb(resolver, managed_env)
    known = {service.id for service in kb.services}
    items = []
    for page in PAGES:
        ids = [service_id for service_id in page["service_ids"] if service_id in known]
        assert ids == page["service_ids"], f"в данных клиники нет услуг: {set(page['service_ids']) - known}"
        items.append({"title": "страница", "status": "approved", **page})
    map_path = managed_env["clients_dir"] / "rosh_import_demo" / "article_service_map.yaml"
    map_path.write_text(yaml.safe_dump({"items": items}, allow_unicode=True), encoding="utf-8")
    resolver.invalidate("rosh_import_demo")
    return resolver.get("rosh_import_demo", fallback=False)


def _ask(kb, message: str, page: str | None, classification=LIST_SERVICES):
    session = Session(company_id="rosh_import_demo", current_page=page)
    return policy.analyze_message(message, session, kb, classification)


def _labels(result) -> list[str]:
    return [action["label"] if isinstance(action, dict) else action for action in result.quick_actions]


def test_service_page_names_its_own_price(resolver, managed_env) -> None:
    kb = _kb(resolver, managed_env)

    result = _ask(kb, OVERVIEW, "/services/biorevitalizaciya")

    text = result.safe_context["message_to_user"]
    assert result.reason == PolicyReason.PRICE_QUESTION
    assert result.service_id == "biorevitalizaciya_9d426f68"  # «Записаться» дальше — на эту услугу
    assert text.startswith("Биоревитализация — от 2 300 до 31 600 ₽.")
    assert text.endswith("Записать вас?")
    assert "Напишите, какая услуга" not in text
    assert _labels(result) == ["Записаться", "Все варианты и цены", "Все цены"]


def test_article_with_price_focus_names_exact_positions(resolver, managed_env) -> None:
    kb = _kb(resolver, managed_env)

    result = _ask(kb, "сколько стоит?", "/blog/sperma-lososya-pdrn", PRICE_NO_SERVICE)

    text = result.safe_context["message_to_user"]
    assert text.startswith("PDRN-препараты — 23 000 ₽.")
    assert "2 300" not in text  # не вся биоревитализация, а только PDRN


def test_focus_outside_the_page_services_is_found_in_the_whole_price_list(resolver, managed_env) -> None:
    kb = _kb(resolver, managed_env)

    result = _ask(kb, OVERVIEW, "/blog/kokki-v-mazke-u-jenschin-chto-eto-znachit-i-chem-opasny")

    # у статьи привязана «Гинекология» (процедуры от 1 000 ₽), а читателю нужна консультация врача
    assert result.safe_context["message_to_user"] == "Консультация гинеколога — 5 000 ₽. Записать вас?"
    assert result.service_id == "konsultacii_b8520924"


def test_problem_page_lists_its_services_without_promising_results(resolver, managed_env) -> None:
    kb = _kb(resolver, managed_env)

    result = _ask(kb, OVERVIEW, "/problems/starenie-koji-ruk")

    text = result.safe_context["message_to_user"]
    assert text.startswith("По этой теме в клинике есть:")
    assert text.count("• ") == 4 and "• Филлеры — от 25 300 ₽" in text
    assert "помога" not in text.lower()
    assert result.service_id is None
    assert _labels(result) == ["Записаться", "Все цены"]


def test_page_outside_the_map_keeps_popular_prices_and_asks_to_book(resolver, managed_env) -> None:
    kb = _kb(resolver, managed_env)

    result = _ask(kb, OVERVIEW, "/kontakty")

    text = result.safe_context["message_to_user"]
    assert text.startswith("Популярные направления и цены:")
    assert text.endswith("Записать вас на консультацию?")
    assert "Напишите, какая услуга" not in text


def test_named_service_and_full_price_list_ignore_the_page(resolver, managed_env) -> None:
    kb = _kb(resolver, managed_env)

    named = _ask(kb, "сколько стоит чистка лица", "/blog/sperma-lososya-pdrn", {
        "intent": "price_question", "service_id": "chistki_e744e513", "confidence": 0.9,
    })
    full = _ask(kb, "покажи все цены", "/blog/sperma-lososya-pdrn")

    assert "PDRN" not in named.safe_context["message_to_user"]
    assert full.safe_context["message_to_user"].startswith("Цены на услуги:")


def test_chat_remembers_the_current_page(test_client) -> None:
    first = test_client.post(
        "/api/chat/message", json={"company_id": "rosh_demo", "session_id": None, "message": "привет", "page": "/blog/a/"}
    ).json()
    test_client.post(
        "/api/chat/message",
        json={"company_id": "rosh_demo", "session_id": first["session_id"], "message": "а цены?", "page": "/services/b?utm=1"},
    )

    session = test_client.portal.call(test_client.app.state.session_store.get, first["session_id"])
    assert session.first_page == "/blog/a"  # где начали — для аналитики
    assert session.current_page == "/services/b"  # где сейчас — для цены
