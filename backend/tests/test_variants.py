"""юнит-тесты на поиск варианта внутри уже выбранной услуги (app/policy/variants.py)."""

from __future__ import annotations

from app.policy.variants import find_variant_matches, should_stay_in_service_context


class _FakeService:
    def __init__(self, name: str, category: str, variants: list[dict[str, object]]) -> None:
        self.name = name
        self.category = category
        self.synonyms: list[str] = []
        self.variants = variants


def _variant(name: str) -> dict[str, object]:
    return {"name": name}


def _laser_piling_service() -> _FakeService:
    # Реальные имена вариантов из услуг.json rosh_import_demo (Лазерный пилинг) — смешанные
    # бренды MicroLaserPeel/NanoLaserPeel, как в живом каталоге.
    names = [
        "Лазерный пилинг MicroLaserPeel - Т зона",
        "Лазерный пилинг MicroLaserPeel - верхняя губа",
        "Лазерный пилинг MicroLaserPeel - внешняя сторона плеча ( за обе, 1 зона)",
        "Лазерный пилинг MicroLaserPeel - внешняя сторона плеча ( за обе, 2 зоны)",
        "Лазерный пилинг MicroLaserPeel - декольте",
        "Лазерный пилинг MicroLaserPeel - зона 5*5 кв.см.",
        "Лазерный пилинг MicroLaserPeel - кисть руки ( одна )",
        "Лазерный пилинг MicroLaserPeel - лицо",
        "Лазерный пилинг MicroLaserPeel - периорбитальная зона ( за обе )",
        "Лазерный пилинг MicroLaserPeel - спина 1 зона ( зона до лопаток/подлопаточная зона/поясничная зона )",
        "Лазерный пилинг MicroLaserPeel - шея",
        "Лазерный пилинг MicroLaserPeel - лицо + шея",
        "Лазерный пилинг NanoLaserPeel - лицо (до 10 микрон)",
    ]
    return _FakeService("Лазерный пилинг", "Лазерный пилинг", [_variant(name) for name in names])


def test_brand_common_to_most_variants_does_not_broaden_match() -> None:
    """Живой баг (нагрузочный тест виджета, 2026-08-25): "microlaserpeel внешняя сторона
    плеча" выдавал ВСЕ варианты вместо 2 релевантных — бренд общий для 12 из 13 вариантов
    не исключался (в отличие от имени самой услуги), поэтому пересекался буквально со всеми."""

    service = _laser_piling_service()

    without_brand = find_variant_matches(service, "внешняя сторона плеча")
    with_brand = find_variant_matches(service, "microlaserpeel внешняя сторона плеча")

    assert len(without_brand) == 2
    assert len(with_brand) == 2
    assert {v["name"] for v in without_brand} == {v["name"] for v in with_brand}


def test_common_word_still_matches_when_it_is_the_only_signal() -> None:
    """Порог "больше половины вариантов", не буквальное пересечение по ВСЕМ — бренд не
    вычёркивается из query_stems, просто перестаёт быть различающим сигналом у вариантов.
    Если у пользователя в запросе ТОЛЬКО общее слово — сервис как таковой уже выбран
    отдельным шагом, тут просто ничего не сузится (пустой список = "уточните ещё")."""

    service = _laser_piling_service()
    result = find_variant_matches(service, "хочу microlaserpeel")
    assert result == []


def test_single_letter_zone_code_matches_after_stop_word_removed() -> None:
    """Живой баг (нагрузочный тест, 2026-08-25): "т зона"/"Т-зона" — стандартный
    косметологический термин (9 вариантов в разных услугах каталога) — раньше давал пустой
    query_stems целиком: "зона" в стоп-словах, а однобуквенный "т" отсекался порогом длины
    >=3, так что ни одного значимого токена не оставалось вообще."""

    service = _laser_piling_service()

    for message in ("т зона", "Т-зона", "т-зона", "Т ЗОНА"):
        result = find_variant_matches(service, message)
        assert len(result) == 1, message
        assert result[0]["name"] == "Лазерный пилинг MicroLaserPeel - Т зона"


def test_two_letter_prepositions_still_filtered_by_length() -> None:
    """Разрешили ИМЕННО однобуквенные токены (не двухбуквенные) — "до"/"от"/"не" и подобные
    предлоги/частицы длиной 2 остаются отфильтрованы порогом длины, как раньше."""

    service = _laser_piling_service()
    # "до" само по себе не должно случайно матчить какой-то вариант просто по факту
    # присутствия в сообщении — вариантов без реального сигнала для сужения тут нет.
    result = find_variant_matches(service, "а что там до зоны")
    assert result == []


def test_face_variant_correctly_returns_both_brands() -> None:
    """"лицо" — реальный, легитимный случай с двумя разными позициями (не баг): у услуги
    есть и MicroLaserPeel, и NanoLaserPeel варианты для лица, оба должны остаться."""

    service = _laser_piling_service()
    result = find_variant_matches(service, "лицо")
    names = {v["name"] for v in result}
    assert "Лазерный пилинг MicroLaserPeel - лицо" in names
    assert "Лазерный пилинг NanoLaserPeel - лицо (до 10 микрон)" in names
    # "лицо + шея" тоже содержит стем "лицо" — легитимно попадает в совпадения.
    assert "Лазерный пилинг MicroLaserPeel - лицо + шея" in names


def test_should_stay_stays_when_message_mentions_current_service_and_a_variant() -> None:
    """Живой баг (нагрузочный тест, 2026-08-25): "пилинг лица" в диалоге про "Лазерный
    пилинг" переключался на другую услугу "Пилинги" — локальный классификатор уверенно
    называет её по слову "пилинг", хотя "лица" уже успешно матчился как вариант ВНУТРИ
    текущей услуги. Остаться в контексте — сообщение упоминает саму услугу ("пилинг" ⊂
    "Лазерный ПИЛИНГ") И реально матчит вариант ("лица" -> "лицо")."""

    service = _laser_piling_service()
    assert should_stay_in_service_context(service, "пилинг лица") is True


def test_should_not_stay_on_genuine_topic_switch_even_with_shared_body_part_word() -> None:
    """Обратный, куда более частый случай — настоящее переключение темы: "а сколько стоит
    чистка лица" в контексте "Лазерный пилинг" тоже находит вариант "лицо" по стему "лиц"
    (общее слово для зон/частей тела встречается почти в любой услуге), но "чистка" никак
    не пересекается с самим "Лазерный пилинг" — значит это реальная смена темы, не наш баг.
    Наивная замена приоритета (без проверки на упоминание САМОЙ услуги) сломала бы именно
    этот случай — бот застрял бы в контексте пилинга вместо переключения на "Чистки"."""

    service = _laser_piling_service()
    assert should_stay_in_service_context(service, "а сколько стоит чистка лица") is False


def test_should_not_stay_when_no_variant_matches_even_if_service_name_mentioned() -> None:
    """Упоминание самой услуги само по себе недостаточно — оба признака нужны вместе."""

    service = _laser_piling_service()
    assert should_stay_in_service_context(service, "а что вообще такое пилинг") is False


def test_should_stay_returns_false_for_missing_service() -> None:
    assert should_stay_in_service_context(None, "пилинг лица") is False


def _fillers_service() -> _FakeService:
    # Реальный паттерн rosh_import_demo: общий префикс "Введение искусственных имплантатов
    # в мягкие ткани" перед КАЖДЫМ брендом — сам бренд единственное различие между вариантами.
    names = [
        "Введение искусственных имплантатов в мягкие ткани Aesthe FillV200 ( 1 флакон )",
        "Введение искусственных имплантатов в мягкие ткани Aliaxin FL ( 1мл. )",
        "Введение искусственных имплантатов в мягкие ткани Belotero Balance ( 1мл. )",
        "Введение искусственных имплантатов в мягкие ткани Belotero Balance с Лидокаином ( 1 мл. )",
        "Введение искусственных имплантатов в мягкие ткани Aliaxin FL с Лидокаином ( 1мл. )",
    ]
    return _FakeService("Филлеры", "Филлеры", [_variant(name) for name in names])


def test_more_specific_query_narrows_instead_of_widening() -> None:
    """Живой баг (нагрузочный тест, 2026-08-25): "belotero balance с лидокаином" выдавал
    4 варианта вместо 1 — "лидокаин" сам по себе пересекается с лидокаиновыми вариантами
    ДРУГИХ брендов (Aliaxin FL с Лидокаином), так что более специфичный запрос расширял
    результат вместо того, чтобы сузить его. Ранжирование по количеству пересечённых слов:
    у точного варианта 3 общих слова (belotero+balance+лидокаин), у чужого бренда — 1."""

    service = _fillers_service()
    result = find_variant_matches(service, "belotero balance с лидокаином")
    assert len(result) == 1
    assert result[0]["name"] == (
        "Введение искусственных имплантатов в мягкие ткани Belotero Balance с Лидокаином ( 1 мл. )"
    )


def test_brand_alone_still_returns_both_its_variants_as_a_tie() -> None:
    """Без уточнения "с лидокаином" — belotero сам по себе одинаково (по 2 слова: brand+
    balance) пересекается с ОБОИМИ вариантами Belotero — законная ничья, оба возвращаются."""

    service = _fillers_service()
    result = find_variant_matches(service, "belotero balance")
    names = {v["name"] for v in result}
    assert names == {
        "Введение искусственных имплантатов в мягкие ткани Belotero Balance ( 1мл. )",
        "Введение искусственных имплантатов в мягкие ткани Belotero Balance с Лидокаином ( 1 мл. )",
    }


def _forever_clear_service() -> _FakeService:
    # Реальный паттерн rosh_import_demo: латинская "T зона" (не кириллическая, как у
    # Лазерного пилинга) — тот же термин, другой алфавит в исходных данных клиники.
    names = [
        "Лазерная терапия Forever Clear - T зона",
        "Лазерная терапия Forever Clear - декольте",
        "Лазерная терапия Forever Clear - лицо",
    ]
    return _FakeService("Лазерная терапия Forever Clear", "Лазерная терапия Forever Clear", [_variant(name) for name in names])


def test_cyrillic_zone_letter_matches_latin_catalog_entry() -> None:
    """Живой баг (нагрузочный тест, 2026-08-25): у "Forever Clear" в каталоге ЛАТИНСКАЯ
    "T зона" — естественный кириллический ввод "т зона" (с русской клавиатуры) не матчил
    вообще, хотя у другой услуги (Лазерный пилинг, кириллица в каталоге) он же работал.
    Однобуквенный код зоны сворачивается в канонический вид независимо от алфавита."""

    service = _forever_clear_service()
    for message in ("т зона", "T зона", "t зона", "Т-зона"):
        result = find_variant_matches(service, message)
        assert len(result) == 1, message
        assert result[0]["name"] == "Лазерная терапия Forever Clear - T зона"
