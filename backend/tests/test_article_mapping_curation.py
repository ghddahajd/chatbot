"""Tests for article mapping curation tooling."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import yaml

from scripts.article_mapping_curation import (
    ArticleMappingDraftLLM,
    ServiceCandidate,
    approve_draft,
    draft_articles,
    escalate_draft,
    reject_draft,
    risk_score_for,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _company_dir(tmp_path: Path) -> Path:
    clients_dir = tmp_path / "clients"
    company_dir = clients_dir / "rosh_import_demo"
    company_dir.mkdir(parents=True)
    (company_dir / "services.json").write_text(
        json.dumps(
            [
                {"id": "svc_topic", "name": "Тематическая услуга", "synonyms": ["тема"]},
                {"id": "svc_compare", "name": "Сравнительная услуга", "synonyms": ["сравнение"]},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_yaml(company_dir / "article_service_map.yaml", {"items": []})
    return clients_dir


def test_risk_score_is_calculated_without_trusting_llm() -> None:
    assert risk_score_for([], []) == "green"
    assert risk_score_for([ServiceCandidate(service_id="svc_topic", relation="topic_related")], []) == "green"
    assert risk_score_for([ServiceCandidate(service_id="svc_topic", relation="unclear")], []) == "yellow"
    assert risk_score_for(
        [ServiceCandidate(service_id="svc_topic", relation="topic_related")],
        ["brand_equipment"],
    ) == "red"


def test_draft_articles_writes_pending_yaml_and_skips_approved_url(tmp_path: Path) -> None:
    clients_dir = _company_dir(tmp_path)
    documents_path = tmp_path / "documents.corpus.jsonl"
    _write_jsonl(
        documents_path,
        [
            {
                "article_id": "approved_article",
                "title": "Уже одобрено",
                "url": "https://example.test/approved/",
                "text": "Текст.",
            },
            {
                "article_id": "new_article",
                "title": "Новая статья",
                "url": "https://example.test/new",
                "text": "Текст про тему.",
            },
        ],
    )
    _write_yaml(
        clients_dir / "rosh_import_demo" / "article_service_map.yaml",
        {
            "items": [
                {
                    "url": "https://example.test/approved",
                    "title": "Уже одобрено",
                    "service_ids": ["svc_topic"],
                    "status": "approved",
                }
            ]
        },
    )

    async def fake_drafter(article, services):
        assert article["article_id"] == "new_article"
        assert services[0]["id"] == "svc_topic"
        return ArticleMappingDraftLLM(
            service_candidates=[
                ServiceCandidate(
                    service_id="svc_topic",
                    relation="topic_related",
                    confidence=0.91,
                    evidence="Текст про тему",
                    reason="Основная тема.",
                )
            ],
            trigger_phrases=["тема пользователя"],
        )

    created = asyncio.run(
        draft_articles(
            company="rosh_import_demo",
            documents_path=documents_path,
            clients_dir=clients_dir,
            drafter=fake_drafter,
        )
    )

    assert len(created) == 1
    draft = _read_yaml(created[0])
    assert draft["status"] == "pending_review"
    assert draft["risk_score"] == "green"
    assert draft["service_candidates"][0]["service_id"] == "svc_topic"
    assert draft["trigger_phrases"] == ["тема пользователя"]


def test_approve_draft_only_allows_topic_related_candidates(tmp_path: Path) -> None:
    mapping_path = tmp_path / "article_service_map.yaml"
    draft_path = tmp_path / "draft.yaml"
    _write_yaml(mapping_path, {"items": []})
    _write_yaml(
        draft_path,
        {
            "url": "https://example.test/article",
            "title": "Статья",
            "status": "pending_review",
            "risk_score": "yellow",
            "service_candidates": [
                {"service_id": "svc_topic", "relation": "topic_related", "confidence": 0.9},
                {"service_id": "svc_compare", "relation": "comparison_only", "confidence": 0.99},
            ],
            "trigger_phrases": ["тема"],
            "risk_flags": [],
            "reviewer_note": "Проверено человеком.",
        },
    )

    approve_draft(draft_path, mapping_path, requested_ids=["svc_topic", "svc_compare"])

    mapping = _read_yaml(mapping_path)
    draft = _read_yaml(draft_path)
    assert mapping["items"][0]["service_ids"] == ["svc_topic"]
    assert mapping["items"][0]["reviewed_note"] == "Проверено человеком."
    assert draft["status"] == "approved"
    assert draft["approved_service_ids"] == ["svc_topic"]


def test_reject_and_escalate_draft_update_state_files(tmp_path: Path) -> None:
    draft_path = tmp_path / "draft.yaml"
    review_path = tmp_path / "needs_medical_review.yaml"
    _write_yaml(
        draft_path,
        {
            "url": "https://example.test/risky",
            "title": "Рискованная статья",
            "status": "pending_review",
            "risk_score": "red",
            "risk_flags": ["adverse_event"],
            "service_candidates": [],
        },
    )

    reject_draft(draft_path)
    assert _read_yaml(draft_path)["status"] == "rejected"

    _write_yaml(
        draft_path,
        {
            "url": "https://example.test/risky",
            "title": "Рискованная статья",
            "status": "pending_review",
            "risk_score": "red",
            "risk_flags": ["adverse_event"],
            "service_candidates": [],
        },
    )
    escalate_draft(draft_path, review_path)

    assert _read_yaml(draft_path)["status"] == "escalated"
    review = _read_yaml(review_path)
    assert review["items"][0]["url"] == "https://example.test/risky"
    assert review["items"][0]["risk_flags"] == ["adverse_event"]
