"""каркас правил: проход по приоритету, «совпало, но ответа нет», запись всех совпавших — и сторож
порядка правил безопасности."""

from __future__ import annotations

from types import SimpleNamespace

from app import policy
from app.models import PolicyAction, PolicyReason, PolicyResult
from app.policy.engine import Decision, Incoming, Rule, run_rules


def _incoming(text: str = "привет") -> Incoming:
    return Incoming(
        message=text, normalized=text, session=None, knowledge_base=None,
        classification={}, intent="unknown", confidence=0.0,
    )


def _answer(reason: PolicyReason):
    return lambda _: PolicyResult(action=PolicyAction.ANSWER, reason=reason)


# ---------------------------------------------------------------- проход


def test_first_matching_rule_wins_and_every_match_is_recorded() -> None:
    rules = (
        Rule("first", when=lambda _: False, answer=_answer(PolicyReason.OK)),
        Rule("second", when=lambda _: True, answer=_answer(PolicyReason.COMPLAINT)),
        Rule("third", when=lambda _: True, answer=_answer(PolicyReason.SMALL_TALK)),
    )

    outcome = run_rules(rules, _incoming())

    assert outcome.rule == "second"
    assert outcome.result.reason == PolicyReason.COMPLAINT
    assert outcome.matched == ["second", "third"]  # третье проиграло — это и есть спор правил


def test_rule_without_answer_passes_the_turn_to_the_next_one() -> None:
    rules = (
        Rule("no_answer", when=lambda _: True, answer=lambda _: None),
        Rule("fallback", when=lambda _: True, answer=_answer(PolicyReason.OK)),
    )

    outcome = run_rules(rules, _incoming())

    assert outcome.rule == "fallback"
    assert outcome.matched == ["no_answer", "fallback"]


def test_no_match_means_no_decision() -> None:
    outcome = run_rules((Rule("never", when=lambda _: False, answer=_answer(PolicyReason.OK)),), _incoming())

    assert (outcome.result, outcome.rule, outcome.matched) == (None, None, [])


def test_answers_after_the_winner_are_not_built() -> None:
    built: list[str] = []

    def answer(name: str):
        def build(_):
            built.append(name)
            return PolicyResult(action=PolicyAction.ANSWER, reason=PolicyReason.OK)
        return build

    rules = (Rule("a", when=lambda _: True, answer=answer("a")), Rule("b", when=lambda _: True, answer=answer("b")))

    run_rules(rules, _incoming())

    assert built == ["a"]  # ответ может быть дорогим (поиск по статьям) — проигравшие его не строят


# ---------------------------------------------------------------- сторож порядка


def test_safety_rules_order_is_deliberate() -> None:
    # Порядок = приоритет. Тест падает при любой перестановке: поменять порядок можно, но только
    # вместе с этим списком и с объяснением в комментарии у SAFETY_RULES.
    assert [rule.name for rule in policy.FIRST_RULES] == ["crisis"]
    assert [rule.name for rule in policy.SAFETY_RULES] == [
        "ambulance_fact",
        "sensitive_topic",
        "complaint",
        "medical",
        "booking_change",
    ]


def test_crisis_is_decided_before_any_parsing(knowledge_base) -> None:
    incoming = Incoming(
        message="не хочу больше жить", normalized="не хочу больше жить", session=None,
        knowledge_base=knowledge_base, classification={}, intent="bot_identity", confidence=0.9,
    )

    outcome = run_rules(policy.FIRST_RULES, incoming)

    assert outcome.rule == "crisis"
    assert outcome.result.reason == PolicyReason.SELF_HARM_CRISIS


# ---------------------------------------------------------------- подпись решения


def test_decision_collects_matches_across_lists_and_keeps_the_winner() -> None:
    decision = Decision()

    decision.note(run_rules((Rule("a", when=lambda _: True, answer=lambda _: None),), _incoming()))
    decision.note(run_rules(
        (Rule("b", when=lambda _: True, answer=_answer(PolicyReason.OK)), Rule("c", when=lambda _: True, answer=_answer(PolicyReason.OK))),
        _incoming(),
    ))

    assert decision.rule == "b"
    assert decision.matched == ["a", "b", "c"]


def test_analyze_message_signs_which_rule_decided(policy_session, knowledge_base) -> None:
    crisis = policy.analyze_message("не хочу больше жить", policy_session, knowledge_base, {"intent": "unknown", "confidence": 0.0})
    legacy = policy.analyze_message("привет", policy_session, knowledge_base, {"intent": "small_talk", "confidence": 0.9})

    assert (crisis.rule, crisis.rules_matched) == ("crisis", ["crisis"])
    assert (legacy.rule, legacy.rules_matched) == (None, [])  # ветка ещё не вынесена в правило


def test_complaint_wins_over_medical_and_the_dispute_is_visible(policy_session, knowledge_base) -> None:
    # Yandex считает «хочу пожаловаться на врача» медицинским вопросом — отвечает всё равно жалоба
    result = policy.analyze_message(
        "хочу пожаловаться на врача", policy_session, knowledge_base, {"intent": "regulated_advice", "confidence": 0.9}
    )

    assert result.rule == "complaint"
    assert result.rules_matched == ["complaint", "medical"]
    assert result.reason == PolicyReason.COMPLAINT


def test_complaint_with_acute_danger_goes_to_medical_with_urgency(policy_session, knowledge_base) -> None:
    message = "буду жаловаться, после укола кровь не останавливается"

    result = policy.analyze_message(message, policy_session, knowledge_base, {"intent": "medical_advice", "confidence": 0.9})

    assert result.rule == "medical"
    assert "complaint" not in result.rules_matched  # жалоба уступает, а не проигрывает спор
    assert policy.escalation_urgency_for(message) == "urgent"


def test_complaint_yields_to_danger_only_when_medicine_takes_it(knowledge_base) -> None:
    # иначе сообщение не досталось бы ни жалобе, ни медицине и упало бы в случайную ветку ниже
    def signals(medical: bool) -> SimpleNamespace:
        return SimpleNamespace(
            normalized="буду жаловаться кровь не останавливается", medical_requested=medical, sensitive_topic=None,
            service=None, knowledge_base=knowledge_base, session=None,
        )

    assert run_rules(policy.SAFETY_RULES, signals(medical=False)).rule == "complaint"
    assert [rule.name for rule in policy.SAFETY_RULES if rule.when(signals(medical=True))] == ["medical"]


def test_urgency_levels() -> None:
    assert policy.escalation_urgency_for("у меня отек лица и тяжело дышать") == "emergency"
    assert policy.escalation_urgency_for("после укола задыхаюсь") == "emergency"
    assert policy.escalation_urgency_for("кровь не останавливается") == "urgent"
    assert policy.escalation_urgency_for("отёк губ после филлера сколько держится") == "calm"


def test_life_threat_is_medical_even_when_the_classifier_missed_it(policy_session, knowledge_base) -> None:
    # местный классификатор (модель недоступна, человек ждёт оператора) видит тут «услугу»
    result = policy.analyze_message("после укола задыхаюсь", policy_session, knowledge_base, {"intent": "service_mention", "confidence": 0.5})

    assert result.rule == "medical"
    assert result.reason == PolicyReason.REGULATED_ADVICE
    assert result.safe_context.get("escalation_urgency") == "emergency"


def test_life_threat_answer_leads_with_the_ambulance(test_client) -> None:
    response = test_client.post("/api/chat/message", json={"company_id": "rosh_demo", "message": "у меня отек лица и тяжело дышать"})

    answer = response.json()["answer"]
    assert answer.startswith("Это может быть опасно")
    assert "103" in answer and "112" in answer
