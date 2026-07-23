"""проверки извлечения контактов из пользовательского мусора."""

from app.models import Service
from app.policy.extractors import extract_name


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
