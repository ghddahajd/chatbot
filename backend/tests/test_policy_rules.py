"""каркас правил: проход по приоритету, «совпало, но ответа нет», запись всех совпавших — и сторож
порядка правил безопасности."""

from __future__ import annotations

from app import policy
from app.models import PolicyAction, PolicyReason, PolicyResult
from app.policy.engine import Incoming, Rule, run_rules


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
        "medical",
        "complaint",
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
