"""шаг 5 дорожной карты: статьи и карта симптомов у каждого клиента свои.

Корпус статей указывается в config.yaml клиента (rag.corpus, путь от RAG_CORPUS_DIR). Нет ключа
или corpus: none — статей нет, чужой корпус не подставляется никогда. Карта «симптом → услуги»
лежит в symptom_service_map.yaml клиента вместо константы в коде.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from app.models import ArticleServiceMapEntry, Session
from app.policy import analyze_message, constants
from app.policy.rules import cosmetic_concern_services

BACKEND_DIR = Path(__file__).resolve().parents[1]
FAQ = {"intent": "faq_question", "service_id": None, "confidence": 0.9}
QUESTION = "как проходит кольпоскопия"
ROSH_CORPUS = "normalized/rosh_articles/crawl/chunks.corpus.jsonl"

# константа из кода до шага 5 — карта РОШ обязана совпасть с ней один в один, с порядком
OLD_ROSH_SYMPTOM_MAP = {
    "акне": ["chistki_e744e513", "konsultacii_b8520924"],
    "жирная кожа": ["chistki_e744e513", "konsultacii_b8520924"],
    "прыщ": ["chistki_e744e513", "konsultacii_b8520924"],
    "сальная кожа": ["chistki_e744e513", "konsultacii_b8520924"],
    "поры": ["chistki_e744e513", "konsultacii_b8520924"],
    "расширенные поры": ["chistki_e744e513", "konsultacii_b8520924"],
    "черные точки": ["chistki_e744e513", "konsultacii_b8520924"],
    "тусклый цвет": ["biorevitalizaciya_9d426f68", "konsultacii_b8520924"],
    "тусклая кожа": ["biorevitalizaciya_9d426f68", "konsultacii_b8520924"],
    "цвет лица": ["biorevitalizaciya_9d426f68", "konsultacii_b8520924"],
    "неровный тон": ["konsultacii_b8520924", "chistki_e744e513"],
    "морщин": ["botulinoterapiya_9d5734af", "biorevitalizaciya_9d426f68", "fillery_f2df3e74"],
    "пигмент": ["fotolechenie_bbl_85e80491", "lazernaya_shlifovka_8965cb81", "pilingi_8dde1279"],
    "папиллом": ["udalenie_novoobrazovanii_12634fed", "konsultacii_b8520924"],
    "бородавк": ["udalenie_novoobrazovanii_12634fed", "konsultacii_b8520924"],
    "шипиц": ["udalenie_novoobrazovanii_12634fed", "konsultacii_b8520924"],
}


def _write_corpus(path: Path, url: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "chunk_id": f"{url}-1",
        "document_id": url,
        "title": "Как проходит кольпоскопия",
        "url": url,
        "chunk_index": 0,
        "source_type": "article",
        "text": (
            "Как проходит кольпоскопия: кольпоскопия помогает врачу осмотреть шейку матки и выявить изменения тканей. "
            "Исследование проводится при помощи специального оптического прибора."
        ),
    }
    path.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _add_client(managed_env, resolver, company_id: str, *, config_extra: str = "", symptom_yaml: str | None = None):
    """копия тестового rosh_demo под новым id, без его rag — каждый тест задаёт свой."""

    clients_dir = managed_env["clients_dir"]
    target = clients_dir / company_id
    shutil.copytree(clients_dir / "rosh_demo", target)
    company = target / "company.yaml"
    company.write_text(
        re.sub(r"^company_id: .*$", f"company_id: {company_id}", company.read_text(encoding="utf-8"), flags=re.M),
        encoding="utf-8",
    )
    config = target / "config.yaml"
    config_text = re.sub(r"^rag:\n(?:  .*\n)+", "", config.read_text(encoding="utf-8"), flags=re.M)
    config.write_text(config_text + config_extra, encoding="utf-8")
    if symptom_yaml is not None:
        (target / "symptom_service_map.yaml").write_text(symptom_yaml, encoding="utf-8")
    return resolver.get(company_id, fallback=False)


def _article_urls(result) -> list[str]:
    return [str(item.get("url")) for item in result.safe_context.get("article_context") or []]


# ---------------------------------------------------------------- корпус статей


def test_client_without_corpus_never_gets_articles_even_if_a_default_corpus_exists(
    managed_env, resolver, tmp_path: Path, monkeypatch
) -> None:
    """та самая утечка: до шага 5 любой клиент искал в корпусе РОШ."""

    rosh_corpus = _write_corpus(tmp_path / "corpora" / ROSH_CORPUS, "https://rosh.test/kolposkopiya")
    monkeypatch.setenv("RAG_CHUNKS_FILE", str(rosh_corpus))
    monkeypatch.setenv("RAG_CORPUS_DIR", str(tmp_path / "corpora"))
    kb = _add_client(managed_env, resolver, "clinic_two")

    result = analyze_message(QUESTION, Session(company_id="clinic_two"), kb, FAQ)

    assert kb.rag_status()["error"] == "not_declared"
    assert _article_urls(result) == []
    assert "rosh.test" not in json.dumps(result.safe_context, ensure_ascii=False, default=str)


def test_each_client_reads_its_own_corpus(managed_env, resolver, tmp_path: Path, monkeypatch) -> None:
    corpora = tmp_path / "corpora"
    _write_corpus(corpora / "a" / "chunks.jsonl", "https://a.test/kolposkopiya")
    _write_corpus(corpora / "b" / "chunks.jsonl", "https://b.test/kolposkopiya")
    monkeypatch.setenv("RAG_CORPUS_DIR", str(corpora))
    kb_a = _add_client(managed_env, resolver, "clinic_a", config_extra="rag:\n  corpus: a/chunks.jsonl\n")
    kb_b = _add_client(managed_env, resolver, "clinic_b", config_extra="rag:\n  corpus: b/chunks.jsonl\n")

    urls_a = _article_urls(analyze_message(QUESTION, Session(company_id="clinic_a"), kb_a, FAQ))
    urls_b = _article_urls(analyze_message(QUESTION, Session(company_id="clinic_b"), kb_b, FAQ))

    assert urls_a == ["https://a.test/kolposkopiya"]
    assert urls_b == ["https://b.test/kolposkopiya"]


@pytest.mark.parametrize("value", ["none", "None", '""', "null"])
def test_corpus_none_means_no_articles(value: str, managed_env, resolver, tmp_path: Path, monkeypatch) -> None:
    _write_corpus(tmp_path / "corpora" / ROSH_CORPUS, "https://rosh.test/kolposkopiya")
    monkeypatch.setenv("RAG_CORPUS_DIR", str(tmp_path / "corpora"))
    kb = _add_client(managed_env, resolver, "clinic_none", config_extra=f"rag:\n  corpus: {value}\n")

    result = analyze_message(QUESTION, Session(company_id="clinic_none"), kb, FAQ)

    assert kb.rag_corpus_path() is None
    assert kb.rag_status() == {"ok": True, "chunk_count": 0, "error": None, "path": None, "disabled": True}
    assert _article_urls(result) == []


@pytest.mark.parametrize("value", ["../outside.jsonl", "a/../../outside.jsonl", "/tmp/outside.jsonl"])
def test_corpus_path_cannot_leave_the_corpus_dir(value: str, managed_env, resolver, tmp_path: Path, monkeypatch) -> None:
    _write_corpus(tmp_path / "outside.jsonl", "https://outside.test/kolposkopiya")
    (tmp_path / "corpora").mkdir()
    monkeypatch.setenv("RAG_CORPUS_DIR", str(tmp_path / "corpora"))
    kb = _add_client(managed_env, resolver, "clinic_escape", config_extra=f"rag:\n  corpus: {value}\n")

    result = analyze_message(QUESTION, Session(company_id="clinic_escape"), kb, FAQ)

    assert kb.rag_corpus_path() is None
    assert kb.rag_status()["error"] == "invalid_path"
    assert _article_urls(result) == []


def test_missing_corpus_file_is_reported_and_the_bot_still_answers(managed_env, resolver, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RAG_CORPUS_DIR", str(tmp_path / "corpora"))
    kb = _add_client(managed_env, resolver, "clinic_missing", config_extra="rag:\n  corpus: nope/chunks.jsonl\n")

    result = analyze_message(QUESTION, Session(company_id="clinic_missing"), kb, FAQ)

    assert kb.rag_status()["error"] == "file_not_found"
    assert _article_urls(result) == []


def test_curated_article_without_excerpt_does_not_read_any_corpus_when_client_has_none(
    managed_env, resolver, monkeypatch
) -> None:
    """второй путь к корпусу — отрывок статьи по URL из карты статей клиента."""

    kb = _add_client(managed_env, resolver, "clinic_curated", config_extra="rag:\n  corpus: none\n")
    entry = ArticleServiceMapEntry(
        url="https://clinic.test/kolposkopiya", title="Кольпоскопия", service_ids=[kb.services[0].id], status="approved"
    )

    def _must_not_be_called(*_args, **_kwargs):
        raise AssertionError("корпус читать нельзя — у клиента его нет")

    monkeypatch.setattr("app.policy.get_opening_excerpt_for_url", _must_not_be_called)
    from app.policy import _article_guidance_result_from_entry

    result = _article_guidance_result_from_entry(kb, entry)

    assert result is not None
    assert "article_guidance_candidate" not in result.safe_context


# ---------------------------------------------------------------- карта симптомов


def test_symptom_constant_is_gone_from_code() -> None:
    assert not hasattr(constants, "COSMETIC_CONCERN_SERVICE_MAP")


def test_symptom_map_comes_from_client_file_normalized_and_in_order(managed_env, resolver) -> None:
    probe = _add_client(managed_env, resolver, "clinic_probe")
    first, second = probe.services[0].id, probe.services[1].id
    kb = _add_client(
        managed_env,
        resolver,
        "clinic_symptoms",
        symptom_yaml=f"Чёрные точки: [{first}]\nакне: [{second}, {first}]\n",
    )

    assert list(kb.symptom_service_map.items()) == [("черные точки", [first]), ("акне", [second, first])]
    assert [service.id for service in cosmetic_concern_services("у меня черные точки", kb)] == [first]


def test_client_without_symptom_map_falls_back_to_its_own_services(managed_env, resolver) -> None:
    kb = _add_client(managed_env, resolver, "clinic_plain")

    services = cosmetic_concern_services("акне, что делать?", kb)

    assert kb.symptom_service_map == {}
    assert {service.id for service in services} <= {service.id for service in kb.services}


@pytest.mark.parametrize("content", ["акне: [a\n  - b: :", "- акне\n- прыщ\n", "акне: чистка\n"])
def test_broken_symptom_map_is_ignored(content: str, managed_env, resolver) -> None:
    kb = _add_client(managed_env, resolver, "clinic_broken", symptom_yaml=content)

    assert kb.symptom_service_map == {}


def test_rosh_symptom_map_is_an_exact_copy_of_the_old_constant() -> None:
    from app.knowledge import KnowledgeBase

    rosh_dir = BACKEND_DIR / "data" / "clients" / "rosh_import_demo"
    if not (rosh_dir / "symptom_service_map.yaml").exists():
        pytest.skip("нет локальной копии данных РОШ")

    kb = KnowledgeBase.load(rosh_dir)

    assert list(kb.symptom_service_map.items()) == list(OLD_ROSH_SYMPTOM_MAP.items())
    assert kb.rag_corpus == ROSH_CORPUS
    assert {sid for ids in kb.symptom_service_map.values() for sid in ids} <= {service.id for service in kb.services}


# ---------------------------------------------------------------- проверки: /health, preflight, запуск, отладка


def test_health_shows_corpus_per_client(test_client, managed_env, resolver, tmp_path: Path, monkeypatch) -> None:
    corpora = tmp_path / "corpora"
    _write_corpus(corpora / ROSH_CORPUS, "https://rosh.test/kolposkopiya")
    monkeypatch.setenv("RAG_CORPUS_DIR", str(corpora))
    _add_client(managed_env, resolver, "clinic_none", config_extra="rag:\n  corpus: none\n")
    _add_client(managed_env, resolver, "clinic_forgot")

    rag = test_client.get("/health").json()["checks"]["rag_index"]

    assert rag["status"] == "degraded"
    assert "clinic_forgot: не указан rag.corpus" in rag["detail"]
    assert "clinic_none: статей нет (none)" in rag["detail"]
    assert "rosh_demo: 1 chunks" in rag["detail"]
    assert rag["chunks_loaded"] == 1
    assert "/" not in rag["detail"]  # пути к файлам на сервере в публичный /health не попадают


def test_preflight_flags_unknown_symptom_service_ids(managed_env, resolver) -> None:
    from app.preflight import _client_summary

    _add_client(managed_env, resolver, "clinic_bad_map", config_extra="rag:\n  corpus: none\n", symptom_yaml="акне: [no_such_service]\n")

    summary = _client_summary(resolver, "clinic_bad_map")

    assert summary["status"] == "degraded"
    assert "no_such_service" in summary["detail"]
    assert summary["symptom_map"] == 1
    assert summary["rag"]["disabled"] is True


def _launch_check_module():
    spec = importlib.util.spec_from_file_location("client_launch_check", BACKEND_DIR / "scripts" / "client_launch_check.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # @dataclass внутри скрипта ищет свой модуль в sys.modules
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("config_extra", "symptom_yaml", "blocked"),
    [
        ("", None, True),  # rag.corpus не указан — клиента не выпускаем
        ("rag:\n  corpus: none\n", None, False),
        ("rag:\n  corpus: none\n", "акне: [no_such_service]\n", True),
    ],
)
def test_launch_check_requires_explicit_corpus_and_known_services(
    config_extra: str, symptom_yaml: str | None, blocked: bool, managed_env, resolver
) -> None:
    module = _launch_check_module()
    _add_client(managed_env, resolver, "clinic_launch", config_extra=config_extra, symptom_yaml=symptom_yaml)
    state = module.CheckState(blockers=[], warnings=[])

    module._check_articles_and_symptoms(managed_env["clients_dir"] / "clinic_launch", state)

    assert bool(state.blockers) is blocked


def test_new_client_templates_declare_no_corpus() -> None:
    for config_path in sorted((BACKEND_DIR / "data" / "client_template").glob("*/config.yaml")):
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert payload.get("rag") == {"corpus": "none"}, config_path


def test_debug_rag_search_uses_the_requested_clients_corpus(
    test_client, managed_env, resolver, tmp_path: Path, monkeypatch
) -> None:
    corpora = tmp_path / "corpora"
    _write_corpus(corpora / "a" / "chunks.jsonl", "https://a.test/kolposkopiya")
    monkeypatch.setenv("RAG_CORPUS_DIR", str(corpora))
    _add_client(managed_env, resolver, "clinic_a", config_extra="rag:\n  corpus: a/chunks.jsonl\n")
    _add_client(managed_env, resolver, "clinic_none", config_extra="rag:\n  corpus: none\n")
    url = "/api/debug/rag-search?token=demo-operator-token"

    found = test_client.post(url, json={"query": QUESTION, "company_id": "clinic_a"})
    refused = test_client.post(url, json={"query": QUESTION, "company_id": "clinic_none"})

    assert found.status_code == 200
    assert [match["url"] for match in found.json()["matches"]] == ["https://a.test/kolposkopiya"]
    assert refused.status_code == 404
