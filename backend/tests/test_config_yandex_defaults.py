"""проверки _apply_llm_provider_defaults — особенно "пустая строка vs переменная отсутствует".

Каждый тест явно передаёт ВСЕ поля, которые эта функция читает (llm_base_url/llm_model/
yandex_folder_id/yandex_api_key) — Settings() иначе читает недостающие поля из реального .env
файла разработчика (pydantic-settings всегда читает env_file для полей без явного kwarg-а),
тесты были бы завязаны на то, что случайно лежит на диске у того, кто их запускает.
"""

from __future__ import annotations

from app.config import Settings, _apply_llm_provider_defaults

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"


def test_yandex_defaults_apply_when_vars_are_truly_absent() -> None:
    """base_url/model на class-дефолте (как локальный .env.example, где строки под Yandex
    просто отсутствуют). Работало и до сегодняшнего фикса — контрольный случай."""

    settings = Settings(
        llm_provider="yandex",
        llm_base_url=_DEFAULT_BASE_URL,
        llm_model=_DEFAULT_MODEL,
        yandex_api_key="key123",
        yandex_folder_id="folder123",
    )

    result = _apply_llm_provider_defaults(settings)

    assert result.llm_base_url == "https://ai.api.cloud.yandex.net/v1"
    assert result.llm_model == "gpt://folder123/aliceai-llm-flash/latest"
    assert result.llm_api_key == "key123"


def test_yandex_defaults_apply_when_vars_are_present_but_blank() -> None:
    """живой репро (аудит §2026-08-22): deploy/.env.production.example оставляет
    LLM_BASE_URL=/LLM_MODEL= пустыми строками, не отсутствующими целиком — pydantic-settings
    это РАЗНЫЕ состояния (empty string задан явно, class default при этом не подставляется).
    "== default"-проверка тут не срабатывает никогда — эта версия шаблона ломала интеграцию
    молча, пока не переехали на "in {'', default}"."""

    settings = Settings(
        llm_provider="yandex",
        llm_base_url="",
        llm_model="",
        yandex_api_key="key123",
        yandex_folder_id="folder123",
    )

    result = _apply_llm_provider_defaults(settings)

    assert result.llm_base_url == "https://ai.api.cloud.yandex.net/v1"
    assert result.llm_model == "gpt://folder123/aliceai-llm-flash/latest"


def test_yandex_explicit_model_override_is_respected() -> None:
    settings = Settings(
        llm_provider="yandex",
        llm_base_url=_DEFAULT_BASE_URL,
        llm_model="aliceai-llm/latest",
        yandex_api_key="key123",
        yandex_folder_id="folder123",
    )

    result = _apply_llm_provider_defaults(settings)

    assert result.llm_model == "gpt://folder123/aliceai-llm/latest"


def test_yandex_without_folder_id_leaves_model_untouched() -> None:
    settings = Settings(
        llm_provider="yandex",
        llm_base_url=_DEFAULT_BASE_URL,
        llm_model=_DEFAULT_MODEL,
        yandex_api_key="key123",
        yandex_folder_id="",
    )

    result = _apply_llm_provider_defaults(settings)

    assert result.llm_model == _DEFAULT_MODEL
    assert result.llm_base_url == "https://ai.api.cloud.yandex.net/v1"
