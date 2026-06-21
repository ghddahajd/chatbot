"""Pydantic models and enums."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class SessionStatus(str, Enum):
    AI_ACTIVE = "AI_ACTIVE"
    WAITING_OPERATOR = "WAITING_OPERATOR"
    HUMAN_ACTIVE = "HUMAN_ACTIVE"
    CLOSED = "CLOSED"


class MessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"
    OPERATOR = "operator"
    SYSTEM = "system"


class PolicyAction(str, Enum):
    ANSWER = "answer"
    ASK_CONTACT = "ask_contact"
    TRANSFER_OPERATOR = "transfer_operator"
    REJECT = "reject"
    CLARIFY = "clarify"
    SMALL_TALK = "small_talk"
    OFF_TOPIC = "off_topic"


class PolicyReason(str, Enum):
    OK = "ok"
    UNKNOWN_SERVICE = "unknown_service"
    SIMILAR_SERVICES_FOUND = "similar_services_found"
    MEDICAL_ADVICE = "medical_advice"
    PRICE_QUESTION = "price_question"
    PRICE_QUESTION_NO_SERVICE = "price_question_no_service"
    OPERATOR_REQUESTED = "operator_requested"
    UNSUPPORTED_CITY = "unsupported_city"
    LOCATION_MISMATCH = "location_mismatch"
    CONTACT_PROVIDED = "contact_provided"
    DURATION_QUESTION = "duration_question"
    SERVICE_EXPLANATION = "service_explanation"
    BOOKING_REQUEST = "booking_request"
    OUT_OF_SCOPE = "out_of_scope"
    SMALL_TALK = "small_talk"
    OFF_TOPIC = "off_topic"


class Message(BaseModel):
    role: MessageRole
    text: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Session(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    company_id: str
    status: SessionStatus = SessionStatus.AI_ACTIVE
    messages: list[Message] = Field(default_factory=list)
    message_count: int = 0
    lead_requested: bool = False
    operator_requested: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Lead(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    session_id: str
    name: str
    phone: str
    summary: str
    service_id: Optional[str] = None


class PriceEntry(BaseModel):
    service_id: str
    price_text: str
    comment: str


class Service(BaseModel):
    id: str
    name: str
    category: str
    synonyms: list[str] = Field(default_factory=list)
    short_description: str
    price_from: Optional[int] = None
    duration: Optional[str] = None
    requires_specialist: bool = True
    source_note: Optional[str] = None


class CompanyConfig(BaseModel):
    company_id: str
    company_name: str
    city: str
    working_hours: str
    phone: str
    address: Optional[str] = None
    website_url: Optional[str] = None
    telegram_url: Optional[str] = None
    allowed_topics: list[str] = Field(default_factory=list)
    operator_triggers: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    medical_disclaimer: str


class PolicyResult(BaseModel):
    action: PolicyAction
    reason: PolicyReason
    service_id: Optional[str] = None
    confidence: float = 0.0
    safe_context: dict[str, Any] = Field(default_factory=dict)
    quick_actions: list[Any] = Field(default_factory=list)


class ChatMessageRequest(BaseModel):
    session_id: Optional[str] = None
    company_id: str
    message: str


class QuickAction(BaseModel):
    label: str
    type: str
    value: str


class ChatMessageResponse(BaseModel):
    session_id: str
    status: SessionStatus
    action: PolicyAction
    answer: str
    lead_created: bool = False
    quick_actions: list[QuickAction] = Field(default_factory=list)


class SessionPublicResponse(BaseModel):
    session_id: str
    company_id: str
    status: SessionStatus
    messages: list[Message]
    lead_requested: bool = False
    operator_requested: bool = False
    updated_at: datetime


class OperatorSessionSummary(BaseModel):
    session_id: str
    status: SessionStatus
    last_message: Optional[str] = None
    updated_at: datetime
