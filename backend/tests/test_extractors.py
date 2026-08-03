"""проверки извлечения контактов из пользовательского мусора."""

from app.knowledge import normalize_text
from app.models import Service
from app.policy.extractors import (
    contains_keyword,
    extract_name,
    find_unsupported_city,
    strip_anaphoric_pronouns,
)


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


def test_find_unsupported_city_does_not_match_word_containing_city_form() -> None:
    # "казани" (форма Казани) — подстрока "противопоказания", но это не тот же токен.
    for message in ["а есть противопоказания", "какие противопоказания у пилинга"]:
        assert find_unsupported_city(normalize_text(message), "Москва") is None


def test_find_unsupported_city_still_matches_real_city_mention() -> None:
    assert find_unsupported_city(normalize_text("а вы в Казани работаете?"), "Москва") == "Казань"


def test_extract_name_handles_greeting_and_filler_words_from_audit() -> None:
    """Раньше "первое слово не из стоп-листа" вытаскивало приветствия/филлеры как имя —
    9 реальных промахов из предзапускового аудита, все — один и тот же класс бага, не
    9 разных. Позитивные маркеры ("зовут"/"это"/"имя"/"я X") решают его целиком, а не
    патчем на каждое конкретное слово."""

    cases = [
        ("меня зовут Анна, телефон +79161234567", "Анна"),
        ("Анна, +79161234567", "Анна"),
        ("+79161234567 Анна", "Анна"),
        ("я Анна, мой номер +79161234567", "Анна"),
        ("Здравствуйте, меня зовут Мария Петровна, +79161234567", "Мария"),
        ("моё имя Ольга +79161234567", "Ольга"),
        ("это Елена, перезвоните +79161234567", "Елена"),
        ("Добрый день! Анна +79161234567", "Анна"),
        ("запишите меня пожалуйста, Ирина, +79161234567", "Ирина"),
    ]
    for message, expected in cases:
        assert extract_name(message, "+79161234567") == expected, message


def test_extract_name_handles_phrasings_not_in_the_original_audit() -> None:
    """Проверка не "подогнана под экзамен" — формулировки, которых не было в исходном
    списке аудита, включая обратный порядок слов ("меня X зовут")."""

    cases = [
        ("привет, я Виктория, +79161234567", "Виктория"),
        ("спасибо, меня Дмитрий зовут, +79161234567", "Дмитрий"),
        ("меня Тимур зовут, вот номер +79161234567", "Тимур"),
        ("имя: Наталья, телефон +79161234567", "Наталья"),
        ("это я, Роман, +79161234567", "Роман"),
    ]
    for message, expected in cases:
        assert extract_name(message, "+79161234567") == expected, message


def test_find_unsupported_city_matches_multiword_city_form() -> None:
    assert (
        find_unsupported_city(normalize_text("а филиал в Санкт Петербурге есть?"), "Москва")
        == "Санкт-Петербург"
    )


def test_strip_anaphoric_pronouns_unblocks_wedged_phrase_match() -> None:
    """'а сколько ЭТО стоит' не матчился с ключом 'сколько стоит' (consecutive-token
    matching), потому что анафора 'это' вклинивается между словами фразы — та же проблема,
    что раньше чинили для 'чем X отличается от Y'."""

    assert strip_anaphoric_pronouns(normalize_text("а сколько это стоит?")) == "а сколько стоит"


def test_strip_anaphoric_pronouns_leaves_normal_messages_untouched() -> None:
    assert strip_anaphoric_pronouns(normalize_text("сколько стоит биоревитализация")) == (
        "сколько стоит биоревитализация"
    )
