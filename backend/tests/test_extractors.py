"""проверки извлечения контактов из пользовательского мусора."""

from app.knowledge import normalize_text
from app.models import Service
from app.policy.extractors import (
    contains_keyword,
    extract_name,
    find_unsupported_city,
    is_location_mismatch,
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
        ("Здравствуйте, меня зовут Мария Петровна, +79161234567", "Мария Петровна"),
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


def test_find_unsupported_city_ner_catches_declension_not_in_known_forms() -> None:
    """Живой пробел: KNOWN_CITY_FORMS для большинства городов содержит только именительный
    падеж — 'живу в Екатеринбурге' (предложный) не ловился словарным путём вообще. NER
    находит LOC-сущность в СЫРОМ сообщении и лемматизирует до именительного падежа — нужен
    оригинальный message (регистр важен для распознавания), не только normalized_message."""

    message = "живу в Екатеринбурге, можно у вас лечиться?"
    assert find_unsupported_city(normalize_text(message), "Москва", message=message) == "Екатеринбург"


def test_find_unsupported_city_ner_catches_multiword_declension() -> None:
    message = "а филиал в Нижнем Новгороде есть?"
    assert find_unsupported_city(normalize_text(message), "Москва", message=message) == "Нижний Новгород"


def test_find_unsupported_city_ner_skips_companys_own_city() -> None:
    """LOC-сущность, совпадающая с городом клиники — не 'не тот город', а просто клиент
    упомянул тот же город, что и клиника."""

    message = "вы в Москве находитесь?"
    assert find_unsupported_city(normalize_text(message), "Москва", message=message) is None


def test_find_unsupported_city_without_raw_message_falls_back_to_dict_only() -> None:
    """message — опциональный параметр (не все вызовы его передают, обратная совместимость) —
    без него NER не запускается, работает только словарный путь, как раньше."""

    message = "живу в Екатеринбурге, можно у вас лечиться?"
    assert find_unsupported_city(normalize_text(message), "Москва") is None


def test_find_unsupported_city_ner_does_not_flag_districts_within_own_city() -> None:
    """Живой регресс, найден внешним аудитом: NER тегирует LOC район/станцию метро/ориентир
    так же, как город — "я с Таганки" (москвич говорит о своём районе) ложно давало "не тот
    город". Предлог перед сущностью — сигнал уровня: "в"/"из" вводят город, "на"/"с" —
    район/ориентир внутри города (та же прилагательная падежная форма в обоих случаях)."""

    for message in [
        "я с Таганки",
        "живу на Соколе",
        "я с Арбата",
        "я рядом с Курской",
        "живу на Юго-Западной",
    ]:
        assert find_unsupported_city(normalize_text(message), "Москва", message=message) is None, message


def test_find_unsupported_city_ner_still_catches_real_city_with_v_or_iz_preposition() -> None:
    """Регрессия не должна съесть настоящие случаи — предлоги 'в'/'из' перед городом
    по-прежнему считаются надёжным сигналом уровня."""

    message = "я из Казани"
    assert find_unsupported_city(normalize_text(message), "Москва", message=message) == "Казань"


def test_strip_anaphoric_pronouns_unblocks_wedged_phrase_match() -> None:
    """'а сколько ЭТО стоит' не матчился с ключом 'сколько стоит' (consecutive-token
    matching), потому что анафора 'это' вклинивается между словами фразы — та же проблема,
    что раньше чинили для 'чем X отличается от Y'."""

    assert strip_anaphoric_pronouns(normalize_text("а сколько это стоит?")) == "а сколько стоит"


def test_strip_anaphoric_pronouns_leaves_normal_messages_untouched() -> None:
    assert strip_anaphoric_pronouns(normalize_text("сколько стоит биоревитализация")) == (
        "сколько стоит биоревитализация"
    )


def test_extract_name_captures_patronymic_when_capitalized_in_original() -> None:
    """Живой случай: 'меня зовут Мария Петровна' раньше обрезалось до одного слова. Заглавная
    буква в оригинале — сигнал, что второе слово тоже имя, а не продолжение фразы."""

    assert extract_name("Здравствуйте, меня зовут Мария Петровна, +79161234567", "+79161234567") == (
        "Мария Петровна"
    )


def test_extract_name_does_not_capture_lowercase_continuation_as_patronymic() -> None:
    """'Мария очень приятно' — 'очень' с маленькой буквы, это продолжение фразы, не отчество."""

    assert extract_name("меня зовут Мария очень приятно", None) == "Мария"


def test_extract_name_reversed_order_also_captures_patronymic() -> None:
    assert extract_name("меня Мария Петровна зовут", None) == "Мария Петровна"


def test_extract_name_bare_reply_still_works_with_greeting_prefix() -> None:
    """Регрессия от сужения fallback-скана: 'привет, я Виктория' — 3 токена, но 'привет'/'я' —
    стоп-слова, по сути это короткий голый ответ (одно значимое слово), должен продолжать
    находить имя, не только фразы ровно из 1-2 слов."""

    assert extract_name("привет, я Виктория, +79161234567", "+79161234567") == "Виктория"


def test_extract_name_ignores_random_word_in_long_unmarked_sentence() -> None:
    """Без явного маркера ('меня зовут'/'я'/'это') и без короткой формы — разросшееся
    сообщение без представления не должно случайно отдавать первое незнакомое слово как имя."""

    assert extract_name("давайте просто идти дальше уже ладно", None) is None


def test_extract_name_still_accepts_short_unmarked_reply() -> None:
    """Короткий голый ответ (без маркера, 1-2 значимых слова) — легитимный кейс 'бот спросил
    как зовут, человек ответил только именем' — должен продолжать работать."""

    assert extract_name("Мария", None) == "Мария"


def test_extract_name_skips_word_famliya_and_finds_actual_surname() -> None:
    """Живой баг: 'моя фамилия Петина' без маркера-паттерна для 'фамилия X' проваливался в
    fallback, где первым незнакомым словом оказывалось само 'фамилия', а не 'Петина'."""

    assert extract_name(
        "Хочу оставить телефон, моя фамилия Петина, номер +79161234567.",
        "+79161234567",
    ) == "Петина"


def test_extract_name_ner_does_not_treat_insult_as_name() -> None:
    """То, что regex в принципе не может: настоящее NER-распознавание отличает оскорбление/
    случайное слово от имени человека, а не угадывает по стоп-листу и заглавным буквам."""

    assert extract_name("дура тупая", None) is None


def test_extract_name_ner_handles_patronymic_natively() -> None:
    """NER находит имя+отчество как единую сущность без ручных костылей на заглавную букву
    второго слова (в отличие от regex-пути, который используется как фолбэк)."""

    assert extract_name("Здравствуйте, меня зовут Мария Петровна, +79161234567", "+79161234567") == (
        "Мария Петровна"
    )


def test_extract_name_falls_back_to_regex_when_ner_finds_nothing() -> None:
    """Единственный найденный пробел NER: голое 'Имя +телефон' без рамки-глагола natasha не
    ловит — здесь должен сработать regex-фолбэк, не потерять имя вообще."""

    assert extract_name("Иван +79991234567", "+79991234567") == "Иван"


def test_extract_name_ner_retry_finds_lowercase_bare_name() -> None:
    """Naташа не ловит имена с маленькой буквы на сыром сообщении вообще ('меня зовут мария'
    тоже мимо) — но если изолировать короткую фразу-кандидата и поднять регистр just у неё,
    находит корректно. Живой случай: 'леха' (маленькая буква, неформальный ответ)."""

    assert extract_name("хочу оставить телефон\n89999229333 леха", "+79999229333") == "Леха"


def test_extract_name_ner_retry_still_rejects_insult_even_capitalized() -> None:
    """Ключевая проверка: изоляция+регистр — это не 'верим заглавной букве' (старый слабый
    компромисс), а НАСТОЯЩЕЕ распознавание. NER продолжает отличать реальное имя от случайного
    слова семантически, даже когда оскорбление тоже подняли в регистр перед проверкой."""

    assert extract_name("дура тупая", None) is None
    assert extract_name("дура", None) is None


def test_is_location_mismatch_ner_recognizes_own_city_outside_hardcoded_dict() -> None:
    """Живой баг (research.md #2): KNOWN_CITY_FORMS вручную заполнен падежами только для
    Москвы/Казани/Питера — 'я из Ярославля' клиенту из Ярославля давало ложный location_mismatch,
    потому что словарь про Ярославль вообще ничего не знает. NER+лемма ловит любой город
    независимо от того, вписан ли он в словарь вручную."""

    cases = [
        ("я из Ярославля, можно к вам записаться?", "Ярославль"),
        ("живу в Ярославле", "Ярославль"),
        ("живу в Перми, а филиал у вас есть?", "Пермь"),
        ("живу в Твери", "Тверь"),
        ("живу в Нижнем Новгороде", "Нижний Новгород"),
    ]
    for message, company_city in cases:
        normalized = normalize_text(message)
        assert is_location_mismatch(message, normalized, company_city) is False, message


def test_is_location_mismatch_still_catches_real_mismatch_for_non_dict_city() -> None:
    """Регрессия не должна съесть настоящий mismatch — если клиника в Ярославле, а пишут из
    Казани, это по-прежнему location_mismatch."""

    message = "я из Казани, можно к вам записаться?"
    assert is_location_mismatch(message, normalize_text(message), "Ярославль") is True


def test_is_location_mismatch_recognizes_bare_peterburg_without_sankt_prefix() -> None:
    """'Петербург' без 'Санкт-' — обиходная форма, которой не было в KNOWN_CITY_FORMS (там
    только полное 'санкт-петербург'/'санкт петербург' и синоним 'питер')."""

    message = "я из Петербурга"
    assert is_location_mismatch(message, normalize_text(message), "Санкт-Петербург") is False
