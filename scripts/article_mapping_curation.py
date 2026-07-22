"""Helpers for drafting and reviewing approved article -> service mappings."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml
from pydantic import BaseModel, Field, ValidationError


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_COMPANY = "rosh_import_demo"
DEFAULT_DOCUMENTS_PATH = REPO_ROOT / "client-input" / "normalized" / "rosh_articles" / "crawl" / "documents.corpus.jsonl"
CLIENTS_DIR = BACKEND_DIR / "data" / "clients"
ARTICLE_MAPPING_FILE = "article_service_map.yaml"
DRAFTS_RELATIVE_DIR = Path("kb") / "article_mappings"
NEEDS_MEDICAL_REVIEW_FILE = "needs_medical_review.yaml"
ARTICLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
APPROVABLE_RELATIONS = {"topic_related"}
BLOCKED_RELATIONS = {
    "comparison_only",
    "contraindication_or_warning",
    "negative_example",
    "brand_or_equipment_mention",
}
RED_RISK_FLAGS = {"contraindication_mentioned", "adverse_event", "brand_equipment"}
FINAL_STATUSES = {"approved", "rejected", "escalated"}


class ServiceCandidate(BaseModel):
    service_id: str = ""
    relation: str = "unclear"
    confidence: float = 0.0
    evidence: str = ""
    reason: str = ""


class ArticleMappingDraftLLM(BaseModel):
    service_candidates: list[ServiceCandidate] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    trigger_phrases: list[str] = Field(default_factory=list)
    reviewer_note: str = ""


class DraftArticleResult(BaseModel):
    url: str
    title: str
    status: str = "pending_review"
    risk_score: str
    service_candidates: list[ServiceCandidate] = Field(default_factory=list)
    approved_service_ids: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    trigger_phrases: list[str] = Field(default_factory=list)
    reviewer_note: str = ""
    generated_at: str


def normalize_url(url: object) -> str:
    return str(url or "").strip().rstrip("/")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        payload = json.loads(line)
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_number}: expected JSON object")
        rows.append(payload)
    return rows


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return payload if isinstance(payload, dict) else {}


def company_dir(company: str, clients_dir: Path = CLIENTS_DIR) -> Path:
    return clients_dir / company


def article_map_path(company: str, clients_dir: Path = CLIENTS_DIR) -> Path:
    return company_dir(company, clients_dir) / ARTICLE_MAPPING_FILE


def drafts_dir(company: str, clients_dir: Path = CLIENTS_DIR) -> Path:
    return company_dir(company, clients_dir) / DRAFTS_RELATIVE_DIR


def draft_path_for_article(article_id: object, drafts_root: Path) -> Path:
    clean_id = str(article_id or "").strip()
    if not clean_id or not ARTICLE_ID_PATTERN.fullmatch(clean_id):
        raise ValueError(f"unsafe article_id: {article_id!r}")
    return drafts_root / f"{clean_id}.yaml"


def load_services_for_prompt(company: str, clients_dir: Path = CLIENTS_DIR) -> list[dict[str, Any]]:
    services_path = company_dir(company, clients_dir) / "services.json"
    services = json.loads(services_path.read_text(encoding="utf-8"))
    if not isinstance(services, list):
        raise ValueError(f"{services_path}: expected list")
    prompt_services = []
    for service in services:
        if not isinstance(service, dict):
            continue
        prompt_services.append(
            {
                "id": service.get("id"),
                "name": service.get("name"),
                "synonyms": service.get("synonyms") or [],
            }
        )
    return prompt_services


def final_decision_urls(company: str, clients_dir: Path = CLIENTS_DIR) -> set[str]:
    urls: set[str] = set()
    mapping = read_yaml(article_map_path(company, clients_dir))
    for item in mapping.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "").strip() in {"approved", "rejected"}:
            url = normalize_url(item.get("url"))
            if url:
                urls.add(url)

    drafts_root = drafts_dir(company, clients_dir)
    if drafts_root.exists():
        for draft_file in drafts_root.glob("*.yaml"):
            draft = read_yaml(draft_file)
            if str(draft.get("status") or "").strip() in FINAL_STATUSES:
                url = normalize_url(draft.get("url"))
                if url:
                    urls.add(url)
    return urls


def risk_score_for(service_candidates: list[ServiceCandidate], risk_flags: Iterable[str]) -> str:
    flags = {str(flag).strip() for flag in risk_flags if str(flag).strip()}
    if flags & RED_RISK_FLAGS:
        return "red"
    if not flags and all(candidate.relation == "topic_related" for candidate in service_candidates):
        return "green"
    return "yellow"


def build_draft_result(article: dict[str, Any], llm_result: ArticleMappingDraftLLM) -> DraftArticleResult:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return DraftArticleResult(
        url=normalize_url(article.get("url")),
        title=str(article.get("title") or "").strip(),
        risk_score=risk_score_for(llm_result.service_candidates, llm_result.risk_flags),
        service_candidates=llm_result.service_candidates,
        risk_flags=[str(flag).strip() for flag in llm_result.risk_flags if str(flag).strip()],
        trigger_phrases=[str(phrase).strip() for phrase in llm_result.trigger_phrases if str(phrase).strip()],
        reviewer_note=str(llm_result.reviewer_note or "").strip(),
        generated_at=now,
    )


def dump_draft(path: Path, draft: DraftArticleResult) -> None:
    write_yaml(path, draft.model_dump())


def load_draft(path: Path) -> dict[str, Any]:
    return read_yaml(path)


def approvable_service_ids(draft: dict[str, Any], requested_ids: Iterable[str] | None = None) -> list[str]:
    allowed = {
        str(candidate.get("service_id") or "").strip()
        for candidate in draft.get("service_candidates") or []
        if isinstance(candidate, dict)
        and str(candidate.get("relation") or "").strip() in APPROVABLE_RELATIONS
        and str(candidate.get("service_id") or "").strip()
    }
    if requested_ids is None:
        return sorted(allowed)
    requested = [str(service_id).strip() for service_id in requested_ids if str(service_id).strip()]
    return [service_id for service_id in requested if service_id in allowed]


def upsert_article_service_map(
    mapping_path: Path,
    draft: dict[str, Any],
    approved_ids: list[str],
) -> None:
    mapping = read_yaml(mapping_path)
    items = mapping.get("items")
    if not isinstance(items, list):
        items = []
    normalized_draft_url = normalize_url(draft.get("url"))
    items = [
        item
        for item in items
        if not (isinstance(item, dict) and normalize_url(item.get("url")) == normalized_draft_url)
    ]
    reviewed_note = str(draft.get("reviewer_note") or "").strip()
    if not reviewed_note:
        reviewed_note = f"Approved via review_mappings.py. risk_score={draft.get('risk_score') or 'unknown'}."
    item = {
        "url": normalized_draft_url,
        "title": str(draft.get("title") or "").strip(),
        "trigger_phrases": [
            str(phrase).strip()
            for phrase in draft.get("trigger_phrases") or []
            if str(phrase).strip()
        ],
        "service_ids": approved_ids,
        "status": "approved",
        "reviewed_note": reviewed_note,
    }
    extra_caution_note = str(draft.get("extra_caution_note") or "").strip()
    if extra_caution_note:
        item["extra_caution_note"] = extra_caution_note
    items.append(item)
    mapping["items"] = items
    write_yaml(mapping_path, mapping)


def mark_draft_status(path: Path, status: str, approved_ids: list[str] | None = None) -> dict[str, Any]:
    draft = load_draft(path)
    draft["status"] = status
    if approved_ids is not None:
        draft["approved_service_ids"] = approved_ids
    write_yaml(path, draft)
    return draft


def append_needs_medical_review(path: Path, draft: dict[str, Any], draft_path: Path) -> None:
    payload = read_yaml(path)
    items = payload.get("items")
    if not isinstance(items, list):
        items = []
    normalized_url = normalize_url(draft.get("url"))
    items = [
        item
        for item in items
        if not (isinstance(item, dict) and normalize_url(item.get("url")) == normalized_url)
    ]
    items.append(
        {
            "url": normalized_url,
            "title": draft.get("title") or "",
            "draft_file": str(draft_path),
            "risk_score": draft.get("risk_score") or "",
            "risk_flags": draft.get("risk_flags") or [],
            "reviewer_note": draft.get("reviewer_note") or "",
        }
    )
    payload["items"] = items
    write_yaml(path, payload)


def approve_draft(draft_path: Path, mapping_path: Path, requested_ids: Iterable[str] | None = None) -> dict[str, Any]:
    draft = load_draft(draft_path)
    approved_ids = approvable_service_ids(draft, requested_ids)
    if not approved_ids:
        raise ValueError("no topic_related service candidates to approve")
    upsert_article_service_map(mapping_path, draft, approved_ids)
    return mark_draft_status(draft_path, "approved", approved_ids)


def reject_draft(draft_path: Path) -> dict[str, Any]:
    return mark_draft_status(draft_path, "rejected")


def escalate_draft(draft_path: Path, review_path: Path) -> dict[str, Any]:
    draft = mark_draft_status(draft_path, "escalated")
    append_needs_medical_review(review_path, draft, draft_path)
    return draft


def pending_draft_files(root: Path, risk: str | None = None) -> list[Path]:
    order = {"green": 0, "yellow": 1, "red": 2}
    files: list[tuple[int, str, Path]] = []
    if not root.exists():
        return []
    for path in root.glob("*.yaml"):
        draft = load_draft(path)
        if draft.get("status") != "pending_review":
            continue
        draft_risk = str(draft.get("risk_score") or "").strip().lower()
        if risk and draft_risk != risk:
            continue
        files.append((order.get(draft_risk, 99), str(draft.get("title") or path.name), path))
    files.sort(key=lambda item: (item[0], item[1]))
    return [path for _, _, path in files]


async def draft_article_with_llm(
    article: dict[str, Any],
    services: list[dict[str, Any]],
    *,
    max_article_chars: int = 12000,
) -> ArticleMappingDraftLLM:
    from app.config import get_settings
    from app.llm.openai_compatible import OpenAIClient
    from app.llm.parsing import tolerant_json_parse
    from app.llm.prompts import ARTICLE_MAPPING_DRAFT_PROMPT

    settings = get_settings()
    if not settings.llm_api_key:
        raise RuntimeError("LLM_API_KEY/OPENAI_API_KEY/GEMINI_API_KEY is required")
    client = OpenAIClient(
        api_key=settings.llm_api_key,
        model=settings.llm_model,
        base_url=settings.llm_base_url,
    )
    services_payload = json.dumps(services, ensure_ascii=False, default=str)
    article_text = str(article.get("text") or "")[:max_article_chars]
    user_payload = (
        f"services_json:\n{services_payload}\n\n"
        f"article_id: {article.get('article_id') or article.get('id') or ''}\n"
        f"title: {article.get('title') or ''}\n"
        f"url: {article.get('url') or ''}\n\n"
        f"article_text:\n{article_text}"
    )
    request_payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": ARTICLE_MAPPING_DRAFT_PROMPT},
            {"role": "user", "content": user_payload},
        ],
        "temperature": 0,
        "max_tokens": 900,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "article_mapping_draft",
                "strict": False,
                "schema": ArticleMappingDraftLLM.model_json_schema(),
            },
        },
    }
    response = await asyncio.wait_for(
        client._post_chat_completions_with_schema_fallback(
            request_payload,
            {"Authorization": f"Bearer {settings.llm_api_key}"},
        ),
        timeout=min(client.timeout, 30.0),
    )
    content = response.json().get("choices", [{}])[0].get("message", {}).get("content", "{}")
    raw_result = tolerant_json_parse(str(content))
    if raw_result is None:
        raise ValueError("LLM returned invalid JSON")
    return ArticleMappingDraftLLM.model_validate(raw_result)


async def draft_articles(
    *,
    company: str,
    documents_path: Path = DEFAULT_DOCUMENTS_PATH,
    clients_dir: Path = CLIENTS_DIR,
    limit: int | None = None,
    overwrite_pending: bool = False,
    drafter: Callable[[dict[str, Any], list[dict[str, Any]]], Any] | None = None,
) -> list[Path]:
    documents = read_jsonl(documents_path)
    services = load_services_for_prompt(company, clients_dir)
    decisions = final_decision_urls(company, clients_dir)
    root = drafts_dir(company, clients_dir)
    root.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    total = len(documents)
    processed = 0
    draft_fn = drafter or draft_article_with_llm

    for index, article in enumerate(documents, start=1):
        if limit is not None and processed >= limit:
            break
        url = normalize_url(article.get("url"))
        if not url or url in decisions:
            continue
        article_id = article.get("article_id") or article.get("id")
        draft_path = draft_path_for_article(article_id, root)
        if draft_path.exists():
            status = str(load_draft(draft_path).get("status") or "").strip()
            if status != "pending_review" or not overwrite_pending:
                continue

        try:
            llm_result = await draft_fn(article, services)
            if not isinstance(llm_result, ArticleMappingDraftLLM):
                llm_result = ArticleMappingDraftLLM.model_validate(llm_result)
            draft = build_draft_result(article, llm_result)
        except (RuntimeError, ValueError, ValidationError) as error:
            print(f"{index}/{total} — {article_id} ❌ {type(error).__name__}: {error}")
            continue

        dump_draft(draft_path, draft)
        processed += 1
        created.append(draft_path)
        print(f"{index}/{total} — {article_id} ✅ {draft.risk_score.upper()} | {draft_path}")

    return created


def review_summary(root: Path) -> dict[str, dict[str, int]]:
    summary = {
        "green": {"done": 0, "pending": 0},
        "yellow": {"done": 0, "pending": 0},
        "red": {"done": 0, "pending": 0},
    }
    if not root.exists():
        return summary
    for path in root.glob("*.yaml"):
        draft = load_draft(path)
        risk = str(draft.get("risk_score") or "yellow").strip().lower()
        if risk not in summary:
            summary[risk] = {"done": 0, "pending": 0}
        if draft.get("status") == "pending_review":
            summary[risk]["pending"] += 1
        else:
            summary[risk]["done"] += 1
    return summary
