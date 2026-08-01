"""проверки извлечения контактов из пользовательского мусора."""

from app.models import Service
from app.policy.extractors import contains_keyword, extract_name


def _service(name: str, *, category: str = "Косметология", synonyms: list[str] | None = None) -> Service:
    return Service(
        id="service-id",
        name=name,
        category=category,
        synonyms=synonyms or [],
        short_description="Описание",
    )


def test_extract_name_does_not_use_service_name_as_person_name() -> None:
    services = [_service("Чистки", synonyms=["чистка лица"])]

    assert extract_name(
        "Чистку +79991234567 завтра утром",
        "+79991234567",
        known_services=services,
    ) is None


def test_extract_name_keeps_real_name_with_known_services() -> None:
    services = [_service("Чистки", synonyms=["чистка лица"])]

    assert (
        extract_name(
            "Иван +79991234567",
            "+79991234567",
            known_services=services,
        )
        == "Иван"
    )


def test_contains_keyword_does_not_match_phrase_across_word_boundary() -> None:
    # "мне нужно" содержит "не нужно" как символьную подстроку ("м" + "не нужно"),
    # но это не один и тот же токен — не должно матчиться как фраза "не нужно".
    assert contains_keyword("мне нужно помыть голову", {"не нужно"}) is False


def test_contains_keyword_still_matches_real_negative_phrase() -> None:
    assert contains_keyword("нет, не нужно, спасибо", {"не нужно"}) is True


def test_contains_keyword_still_matches_truncated_stem_phrase() -> None:
    # "открывае" — намеренно обрезанный стем для "открываете"/"открываетесь" и т.д.
    assert contains_keyword("во сколько открываетесь", {"во сколько открывае"}) is True
