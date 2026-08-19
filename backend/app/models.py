"""pydantic-модели и перечисления."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator


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
    REGULATED_ADVICE = "regulated_advice"
    MEDICAL_ADVICE = "regulated_advice"
    PRICE_QUESTION = "price_question"
    PRICE_QUESTION_NO_SERVICE = "price_question_no_service"
    OPERATOR_REQUESTED = "operator_requested"
    UNSUPPORTED_CITY = "unsupported_city"
    LOCATION_MISMATCH = "location_mismatch"
    CONTACT_PROVIDED = "contact_provided"
    DURATION_QUESTION = "duration_question"
    SERVICE_EXPLANATION = "service_explanation"
    FAQ_QUESTION = "faq_question"
    BOOKING_REQUEST = "booking_request"
    OUT_OF_SCOPE = "out_of_scope"
    SMALL_TALK = "small_talk"
    OFF_TOPIC = "off_topic"
    OFF_TOPIC_BODY_REDIRECT = "off_topic_body_redirect"
    OBJECTION_HANDLED = "objection_handled"
    OBJECTION_BACKOFF = "objection_backoff"
    COMPLAINT = "complaint"


class PendingAction(str, Enum):
    COLLECT_CONTACT = "collect_contact"
    BOOKING_CONTACT = "booking_contact"
    OFFERED_OPERATOR = "offered_operator"


class Message(BaseModel):
    role: MessageRole
    text: str
    kind: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ContextFrame(BaseModel):
    frame_type: str
    entity_type: Optional[str] = None
    entity_id: Optional[str] = None
    entity_label: Optional[str] = None
    slots: dict[str, Any] = Field(default_factory=dict)
    last_intent: Optional[str] = None
    expires_at_turn: Optional[int] = None


class Session(BaseModel):
    session_id: str = Field(default_factory=lambda: str(uuid4()))
    company_id: str
    status: SessionStatus = SessionStatus.AI_ACTIVE
    messages: list[Message] = Field(default_factory=list)
    message_count: int = 0
    substantive_message_count: int = 0
    engagement_offer_count: int = 0
    # По теме возражения (price/hesitation/competitor/guarantee/pain_fear), не общий счётчик —
    # иначе второе возражение по НОВОЙ теме сразу получает backoff из-за старой (реальный баг,
    # найден 2026-08-19). Словарь сам расширяется на будущие темы без правок кода.
    objection_response_counts: dict[str, int] = Field(default_factory=dict)
    # Короткие гарантированные факты, накопленные по ходу разговора (жалоба/деликатная тема/
    # возражение) — показываются оператору ОТДЕЛЬНО от LLM-суммаризатора лида, который на
    # маленькой модели надёжно теряет/смазывает именно такие детали (см. чат от 2026-08-10).
    notable_flags: list[str] = Field(default_factory=list)
    lead_requested: bool = False
    operator_requested: bool = False
    pending_action: Optional[str] = None
    last_service_id: Optional[str] = None
    last_intent: Optional[str] = None
    active_frame: Optional[ContextFrame] = None
    contact_draft: dict[str, Any] = Field(default_factory=dict)
    telegram_topic_id: Optional[int] = None
    telegram_claimed_by: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Lead(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    company_id: str
    session_id: str
    name: str
    phone: str
    summary: str
    service_id: Optional[str] = None
    reason: str = "commercial_interest"
    needs_operator: bool = False
    lead_trigger: str = "ask_contact"
    unresolved_query: str = ""
    recent_messages: list[dict[str, str]] = Field(default_factory=list)
    operator_url: str = ""


class PriceEntry(BaseModel):
    service_id: str
    price_text: str
    comment: str


class ArticleServiceMapEntry(BaseModel):
    url: str
    title: str
    trigger_phrases: list[str] = Field(default_factory=list)
    service_ids: list[str] = Field(default_factory=list)
    status: str = ""
    excerpt: str = ""
    reviewed_note: str = ""
    extra_caution_note: str = ""


class Service(BaseModel):
    id: str
    name: str
    category: str
    synonyms: list[str] = Field(default_factory=list)
    short_description: str
    price_from: Optional[int] = None
    price_to: Optional[int] = None
    price_range_text: Optional[str] = None
    duration: Optional[str] = None
    requires_specialist: bool = True
    source_note: Optional[str] = None
    page_url: Optional[str] = None
    variants: list[dict[str, Any]] = Field(default_factory=list)


class CompanyConfig(BaseModel):
    company_id: str
    company_name: str
    city: str
    working_hours: str
    phone: str
    address: Optional[str] = None
    website_url: Optional[str] = None
    telegram_url: Optional[str] = None
    privacy_policy_url: Optional[str] = None
    lead_webhook_url: Optional[str] = None
    allowed_domains: list[str] = Field(default_factory=list)
    allowed_topics: list[str] = Field(default_factory=list)
    operator_triggers: list[str] = Field(default_factory=list)
    forbidden_claims: list[str] = Field(default_factory=list)
    safety_disclaimer: str = ""
    medical_disclaimer: str = ""

    @model_validator(mode="after")
    def fill_legacy_disclaimers(self) -> "CompanyConfig":
        """синхронизирует новый safety_disclaimer со старым medical_disclaimer."""

        safety_disclaimer = self.safety_disclaimer.strip()
        medical_disclaimer = self.medical_disclaimer.strip()
        if not safety_disclaimer and medical_disclaimer:
            self.safety_disclaimer = medical_disclaimer
        elif not medical_disclaimer and safety_disclaimer:
            self.medical_disclaimer = safety_disclaimer
        elif not safety_disclaimer and not medical_disclaimer:
            fallback = "По этому вопросу лучше уточнить у менеджера."
            self.safety_disclaimer = fallback
            self.medical_disclaimer = fallback
        return self


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
    # без ограничения принимался текст любой длины — попадал в историю сессии, в промпт LLM
    # и в логи целиком. 4000 символов — с запасом под реальные вопросы, но не безлимит.
    message: str = Field(max_length=4000)


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


class WidgetBootstrapResponse(BaseModel):
    company_id: str
    company_name: str
    city: str
    website_url: Optional[str] = None
    telegram_url: Optional[str] = None
    privacy_policy_url: Optional[str] = None
    features: dict[str, bool] = Field(default_factory=dict)
    widget_config: dict[str, Any] = Field(default_factory=dict)
    greeting: str = ""


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
    company_id: str
    status: SessionStatus
    last_message: Optional[str] = None
    updated_at: datetime
