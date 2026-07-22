"""Regression tests for fuzzy KB matching."""

import shutil
from pathlib import Path

from app.knowledge import KnowledgeBaseResolver, _token_prefix_match
from app.models import PolicyAction
from app.policy import analyze_message
from app.policy.intent import classify_and_extract


def _copy_rosh_import_kb(resolver: KnowledgeBaseResolver, managed_env):
    source_dir = Path("backend/data/clients/rosh_import_demo")
    target_dir = managed_env["clients_dir"] / "rosh_import_demo"
    shutil.copytree(source_dir, target_dir)
    return resolver.get("rosh_import_demo", fallback=False)


def _classify(message: str, knowledge_base):
    return classify_and_extract(
        message,
        [service.model_dump() for service in knowledge_base.services],
        knowledge_base.company.city,
        knowledge_base.domain_profile,
    )


def test_token_prefix_does_not_merge_bioresonance_and_biorevitalization() -> None:
    assert not _token_prefix_match("биорезонансная", "биоревитализация")


def test_bioresonance_does_not_resolve_to_biorevitalization(resolver, managed_env) -> None:
    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)

    result = _classify("в чем заключается биорезонансная диагностика", knowledge_base)

    assert result["service_id"] != "biorevitalizaciya_9d426f68"


def test_biorevit_short_form_still_resolves_to_biorevitalization(resolver, managed_env) -> None:
    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)

    result = _classify("сколько биоревит", knowledge_base)

    assert result["service_id"] == "biorevitalizaciya_9d426f68"


def test_bioresonance_question_defers_in_policy(policy_session, resolver, managed_env) -> None:
    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    classification = _classify("в чем заключается биорезонансная диагностика", knowledge_base)

    result = analyze_message(
        "в чем заключается биорезонансная диагностика",
        policy_session,
        knowledge_base,
        classification,
    )

    assert result.action == PolicyAction.CLARIFY
    assert result.service_id != "biorevitalizaciya_9d426f68"


def test_missing_article_service_map_is_empty(knowledge_base) -> None:
    assert knowledge_base.article_service_map == {}


def test_article_service_map_loads_approved_entries(resolver, managed_env) -> None:
    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)

    entry = knowledge_base.article_service_map[
        "https://www.medcenterrosh.ru/blog/vtoroi-podborodok-prichiny-poyavleniya-i-metody-korrekcii"
    ]

    assert entry.title == "Второй подбородок: причины появления"
    assert entry.service_ids == ["lazernaya_terapiya_skin_tyte_40372815", "fillery_f2df3e74"]


def test_article_service_map_loads_optional_excerpt(resolver, managed_env) -> None:
    source_dir = Path("backend/data/clients/rosh_import_demo")
    target_dir = managed_env["clients_dir"] / "rosh_import_demo"
    shutil.copytree(source_dir, target_dir)
    map_path = target_dir / "article_service_map.yaml"
    text = map_path.read_text(encoding="utf-8")
    text = text.replace(
        "  status: approved\n  reviewed_note:",
        "  status: approved\n  excerpt: Одобренный короткий фрагмент статьи.\n  reviewed_note:",
        1,
    )
    map_path.write_text(text, encoding="utf-8")

    knowledge_base = resolver.get("rosh_import_demo", fallback=False)
    entry = knowledge_base.article_service_map[
        "https://www.medcenterrosh.ru/blog/vtoroi-podborodok-prichiny-poyavleniya-i-metody-korrekcii"
    ]

    assert entry.excerpt == "Одобренный короткий фрагмент статьи."


def test_cosmetic_concern_can_use_approved_article_service_mapping(
    monkeypatch,
    policy_session,
    resolver,
    managed_env,
) -> None:
    import app.policy as policy_module

    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    monkeypatch.setattr(policy_module, "cosmetic_concern_services", lambda message, knowledge_base: [])
    monkeypatch.setattr(
        policy_module,
        "_retrieve_article_context_safe",
        lambda message: [
            {
                "title": "Второй подбородок: причины появления",
                "url": (
                    "https://www.medcenterrosh.ru/blog/"
                    "vtoroi-podborodok-prichiny-poyavleniya-i-metody-korrekcii/"
                ),
                "snippet": "Skin Tyte и контурная пластика.",
                "chunk_id": "test-chunk",
                "score": 9.5,
            }
        ],
    )

    result = analyze_message(
        "второй подбородок можно убрать?",
        policy_session,
        knowledge_base,
        {"intent": "cosmetic_concern", "service_id": None, "confidence": 0.82},
    )

    answer = result.safe_context["message_to_user"]
    assert result.action == PolicyAction.ANSWER
    assert result.safe_context["question_type"] == "cosmetic_article_guidance"
    assert "Второй подбородок" in answer
    assert "Лазерная Терапия Skin Tyte" in answer
    assert "Филлеры" in answer
    assert "вам нужно" not in answer.lower()
    assert "вам подходит" not in answer.lower()
    assert result.safe_context["article_context"][0]["chunk_id"] == "test-chunk"
    assert result.quick_actions[0]["label"] == "Лазерная Терапия Skin Tyte"


def test_unknown_service_can_use_article_trigger_phrase(
    monkeypatch,
    policy_session,
    resolver,
    managed_env,
) -> None:
    import app.policy as policy_module

    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    monkeypatch.setattr(policy_module, "similar_services_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(policy_module, "_retrieve_article_context_safe", lambda message: [])

    result = analyze_message(
        "темные круги под глазами, что посоветуете?",
        policy_session,
        knowledge_base,
        {"intent": "unknown_service", "service_id": None, "confidence": 0.9},
    )

    answer = result.safe_context["message_to_user"]
    assert result.action == PolicyAction.ANSWER
    assert result.safe_context["question_type"] == "cosmetic_article_guidance"
    assert result.safe_context["article_service_mapping"]["matched_phrase"] == "темные круги"
    assert "article_guidance_candidate" not in result.safe_context
    assert "Мезотерапия" in answer
    assert "Биоревитализация" in answer
    assert "Филлеры" in answer
    assert "Тёмные круги могут быть связаны не только с косметологией" in answer


def test_regulated_without_hard_signal_can_use_article_trigger_phrase(
    monkeypatch,
    policy_session,
    resolver,
    managed_env,
) -> None:
    import app.policy as policy_module

    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    monkeypatch.setattr(policy_module, "_retrieve_article_context_safe", lambda message: [])

    result = analyze_message(
        "выпадают волосы, что можно сделать?",
        policy_session,
        knowledge_base,
        {"intent": "regulated_advice", "service_id": None, "confidence": 0.95},
    )

    answer = result.safe_context["message_to_user"]
    assert result.action == PolicyAction.ANSWER
    assert result.safe_context["question_type"] == "cosmetic_article_guidance"
    assert result.safe_context["article_service_mapping"]["matched_phrase"] == "выпадают волосы"
    assert "Мезотерапия" in answer


def test_regulated_with_hard_signal_does_not_use_article_trigger_phrase(
    monkeypatch,
    policy_session,
    resolver,
    managed_env,
) -> None:
    import app.policy as policy_module

    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    monkeypatch.setattr(policy_module, "_retrieve_article_context_safe", lambda message: [])

    result = analyze_message(
        "выпадают волосы, но еще температура",
        policy_session,
        knowledge_base,
        {"intent": "regulated_advice", "service_id": None, "confidence": 0.95},
    )

    assert result.safe_context.get("question_type") != "cosmetic_article_guidance"


def test_cosmetic_concern_ignores_unapproved_article_mapping(
    monkeypatch,
    policy_session,
    resolver,
    managed_env,
) -> None:
    import app.policy as policy_module

    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    monkeypatch.setattr(policy_module, "cosmetic_concern_services", lambda message, knowledge_base: [])
    monkeypatch.setattr(
        policy_module,
        "_retrieve_article_context_safe",
        lambda message: [
            {
                "title": "Непромодерированная статья",
                "url": "https://www.medcenterrosh.ru/blog/not-reviewed",
                "snippet": "Не используем для услуги.",
                "chunk_id": "test-chunk",
                "score": 9.5,
            }
        ],
    )

    result = analyze_message(
        "что можно сделать с другой темой?",
        policy_session,
        knowledge_base,
        {"intent": "cosmetic_concern", "service_id": None, "confidence": 0.82},
    )

    assert result.safe_context.get("question_type") != "cosmetic_article_guidance"
