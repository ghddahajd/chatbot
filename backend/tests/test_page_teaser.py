"""приглашение у кнопки чата под страницу: свой текст страницы, иначе по типу страницы, иначе общее."""

from __future__ import annotations

import yaml

from .test_policy import _copy_rosh_import_kb

SITE = "https://www.medcenterrosh.ru"
PAGES = [
    {
        "url": f"{SITE}/blog/sperma-lososya-pdrn",
        "service_ids": ["biorevitalizaciya_9d426f68"],
        "teaser_title": "Узнайте цену PDRN-терапии и запишитесь на консультацию",
    },
    {"url": f"{SITE}/blog/kak-vosstanovit-mikrofloru-vlagalischa", "service_ids": ["ginekologiya_fd7d28b2"]},
    {"url": f"{SITE}/services/biorevitalizaciya/", "service_ids": ["biorevitalizaciya_9d426f68"]},
    {"url": f"{SITE}/services/biorevitalizaciya-plinest", "service_ids": ["biorevitalizaciya_9d426f68"], "price_focus_title": "Plinest"},
    {"url": f"{SITE}/problems/starenie-koji-ruk", "service_ids": ["fillery_f2df3e74", "mezoterapiya_a62ea8d4"]},
    {"url": f"{SITE}/blog/pochemu-shelushitsya-koja", "service_ids": ["konsultacii_b8520924"]},
]


def _teaser(test_client, page: str | None) -> str:
    params = {"company_id": "rosh_import_demo"}
    if page is not None:
        params["page"] = page
    response = test_client.get("/api/widget/bootstrap", params=params)
    assert response.status_code == 200
    return response.json()["teaser_title"]


def _setup(test_client, resolver, managed_env) -> None:
    _copy_rosh_import_kb(resolver, managed_env)
    items = [{"title": "страница", "status": "approved", **page} for page in PAGES]
    map_path = managed_env["clients_dir"] / "rosh_import_demo" / "article_service_map.yaml"
    map_path.write_text(yaml.safe_dump({"items": items}, allow_unicode=True), encoding="utf-8")
    test_client.app.state.knowledge_base_resolver.invalidate("rosh_import_demo")


def test_page_with_its_own_text_gets_it(test_client, resolver, managed_env) -> None:
    _setup(test_client, resolver, managed_env)

    assert _teaser(test_client, "/blog/sperma-lososya-pdrn") == "Узнайте цену PDRN-терапии и запишитесь на консультацию"


def test_page_type_decides_when_there_is_no_own_text(test_client, resolver, managed_env) -> None:
    _setup(test_client, resolver, managed_env)

    assert _teaser(test_client, "/blog/kak-vosstanovit-mikrofloru-vlagalischa") == "Вопрос к гинекологу? Запишу на консультацию"
    assert _teaser(test_client, "/services/biorevitalizaciya") == "Биоревитализация: подскажу цену и запишу"
    # у страницы конкретного препарата — его название, а не всей услуги
    assert _teaser(test_client, "/services/biorevitalizaciya-plinest") == "Plinest: подскажу цену и запишу"
    assert _teaser(test_client, "/problems/starenie-koji-ruk") == "Подберу процедуру и подскажу цены"
    # «Консультации: подскажу цену» звучит коряво — у страниц про приём врача свой текст
    assert _teaser(test_client, "/blog/pochemu-shelushitsya-koja") == "Подскажу, к какому врачу записаться и сколько стоит приём"


def test_page_path_is_matched_without_marks_and_trailing_slash(test_client, resolver, managed_env) -> None:
    _setup(test_client, resolver, managed_env)

    assert _teaser(test_client, "/services/biorevitalizaciya/?utm_source=ya#prices") == "Биоревитализация: подскажу цену и запишу"


def test_unknown_page_keeps_the_usual_teaser(test_client, resolver, managed_env) -> None:
    _setup(test_client, resolver, managed_env)

    # пусто — виджет оставляет своё «Подскажу цену и запишу»
    assert _teaser(test_client, "/kontakty") == ""
    assert _teaser(test_client, None) == ""
