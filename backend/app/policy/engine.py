"""каркас правил: что известно о сообщении, правило «когда + ответ» и проход по списку правил
в порядке приоритета.

Правило — две функции. `when` — дешёвая проверка без побочных эффектов: её зовут у ВСЕХ правил
списка, чтобы было видно, какие правила совпали одновременно и кто кого перекрыл. `answer` — сам
ответ; None значит «совпало, но сказать нечего», и проход идёт к следующему правилу.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Generic, Optional, Sequence, TypeVar

from ..knowledge import KnowledgeBase
from ..models import PolicyResult, Service, Session


@dataclass
class Incoming:
    """сообщение до разбора — хватает правилам, которые обязаны сработать раньше всего остального."""

    message: str
    normalized: str
    session: Session
    knowledge_base: KnowledgeBase
    classification: dict[str, object]
    intent: str
    confidence: float


@dataclass
class Signals(Incoming):
    """сообщение после разбора: найденная услуга, телефон и признаки того, о чём просят."""

    service: Optional[Service]
    phone: Optional[str]
    operator_requested: bool
    operator_consent_given: bool
    duration_requested: bool
    explanation_requested: bool
    price_requested: bool
    booking_requested: bool
    lead_requested: bool
    booking_mentions_clinic_doctor: bool
    is_restricted: bool
    restricted_category: Optional[str]
    medical_requested: bool
    known_service_text: str
    unsupported_city: Optional[str]
    city_in_text: str
    sensitive_topic: Optional[dict[str, object]]


Context = TypeVar("Context", bound=Incoming)


@dataclass(frozen=True)
class Rule(Generic[Context]):
    name: str
    when: Callable[[Context], bool]
    answer: Callable[[Context], Optional[PolicyResult]]


@dataclass
class RuleOutcome:
    result: Optional[PolicyResult]
    rule: Optional[str]
    # все совпавшие по `when`, включая проигравших, — по ним видно, где правила спорят
    matched: list[str] = field(default_factory=list)


def run_rules(rules: Sequence[Rule[Context]], context: Context) -> RuleOutcome:
    """решение — первое по порядку совпавшее правило, у которого нашёлся ответ."""

    hits = [rule for rule in rules if rule.when(context)]
    matched = [rule.name for rule in hits]
    for rule in hits:
        result = rule.answer(context)
        if result is not None:
            return RuleOutcome(result=result, rule=rule.name, matched=matched)
    return RuleOutcome(result=None, rule=None, matched=matched)
