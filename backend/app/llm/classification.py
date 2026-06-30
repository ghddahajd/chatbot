"""pydantic-контракт для structured intent classifier."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


Intent = Literal[
    "small_talk",
    "off_topic",
    "list_services",
    "price_question",
    "regulated_advice",
    "operator_request",
    "booking_request",
    "lead_request",
    "contact_link",
    "service_mention",
    "unknown_service",
    "location_mismatch",
    "faq_question",
]
Risk = Literal[
    "safe",
    "regulated_advice",
    "emergency",
    "off_topic",
    "prompt_injection",
    "unsupported",
]
ServiceMatchType = Literal["none", "exact", "ambiguous", "unknown"]


class IntentClassification(BaseModel):
    """структурированный результат классификации без бизнес-решений."""

    model_config = ConfigDict(extra="ignore")

    intent: Intent
    risk: Risk = "safe"
    service_id: str | None = None
    service_match_type: ServiceMatchType = "none"
    candidate_service_ids: list[str] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0.0, le=1.0)
    needs_clarification: bool = False
    extracted_phone: str | None = None
    extracted_name: str | None = None
    reason_code: str = Field(default="unknown", max_length=80)

    @field_validator(
        "intent",
        "risk",
        "service_match_type",
        "service_id",
        "extracted_phone",
        "extracted_name",
        "reason_code",
        mode="before",
    )
    @classmethod
    def _normalize_string_fields(cls, value: Any) -> Any:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned.lower() if cleaned in {"NONE", "NULL", "Null"} else cleaned
        return value

    @field_validator("candidate_service_ids", mode="before")
    @classmethod
    def _normalize_candidates(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            return []
        result: list[str] = []
        for item in value:
            if item is None:
                continue
            candidate = str(item).strip()
            if candidate and candidate not in result:
                result.append(candidate)
        return result[:3]

    @model_validator(mode="after")
    def _align_service_fields(self) -> "IntentClassification":
        if self.service_id in {"", "null", "none"}:
            self.service_id = None

        if self.intent == "unknown_service":
            self.service_id = None
            self.service_match_type = "unknown"

        if self.service_match_type != "exact":
            self.service_id = None

        if self.service_match_type in {"ambiguous", "unknown"}:
            self.needs_clarification = True

        if self.risk in {"regulated_advice", "emergency", "off_topic", "prompt_injection"}:
            self.needs_clarification = self.risk != "safe"

        return self


def normalize_intent_classification(
    raw_result: dict[str, Any],
    known_service_ids: set[str],
) -> IntentClassification:
    """валидирует и нормализует structured output модели."""

    classification = IntentClassification.model_validate(raw_result)
    known_ids = {str(service_id) for service_id in known_service_ids}

    if classification.service_id and classification.service_id not in known_ids:
        classification.service_id = None
        classification.service_match_type = "unknown"
        classification.needs_clarification = True

    filtered_candidates: list[str] = []
    for candidate in classification.candidate_service_ids:
        if candidate in known_ids and candidate not in filtered_candidates:
            filtered_candidates.append(candidate)
    classification.candidate_service_ids = filtered_candidates[:3]

    if classification.service_match_type == "exact" and not classification.service_id:
        classification.service_match_type = "ambiguous" if filtered_candidates else "unknown"
        classification.needs_clarification = True

    return classification


def safe_normalize_intent_classification(
    raw_result: dict[str, Any],
    known_service_ids: set[str],
) -> IntentClassification | None:
    """возвращает none вместо исключения, чтобы caller мог уйти в fallback."""

    try:
        return normalize_intent_classification(raw_result, known_service_ids)
    except ValidationError:
        return None


def classification_to_legacy_result(classification: IntentClassification) -> dict[str, object]:
    """адаптирует новый контракт под текущий policy flow."""

    intent = classification.intent
    if classification.risk in {"regulated_advice", "emergency"}:
        intent = "medical_advice"
    elif classification.risk == "off_topic":
        intent = "off_topic"
    elif intent in {"regulated_advice", "faq_question"}:
        intent = "service_mention"

    service_id = classification.service_id
    if classification.service_match_type != "exact":
        service_id = None

    return {
        "intent": intent,
        "service_id": service_id,
        "confidence": classification.confidence,
    }
