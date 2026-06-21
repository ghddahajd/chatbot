"""LLM parsing helpers."""

from __future__ import annotations

import json
from typing import Any

from ..policy.constants import ALLOWED_CLASSIFIER_INTENTS


def tolerant_json_parse(raw_text: str) -> dict[str, Any] | None:
    """Parse JSON from local/cloud models that may wrap it in extra text."""

    cleaned_text = raw_text.strip()
    if cleaned_text.startswith("```"):
        lines = cleaned_text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned_text = "\n".join(lines).strip()

    for candidate in (cleaned_text,):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        return parsed if isinstance(parsed, dict) else None

    start = cleaned_text.find("{")
    end = cleaned_text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(cleaned_text[start : end + 1])
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    return None


def normalize_classification_result(
    raw_result: dict[str, Any],
    known_services: list[dict[str, str]],
) -> dict[str, object]:
    """Validate model JSON so policy only sees supported values."""

    known_service_ids = {str(service.get("id")) for service in known_services}

    intent = str(raw_result.get("intent") or "service_mention").strip().lower()
    if intent not in ALLOWED_CLASSIFIER_INTENTS:
        intent = "service_mention"

    service_id = raw_result.get("service_id")
    if service_id is not None:
        service_id = str(service_id).strip()
    if not service_id or service_id == "null" or service_id not in known_service_ids:
        service_id = None

    try:
        confidence = float(raw_result.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)

    return {"intent": intent, "service_id": service_id, "confidence": confidence}
