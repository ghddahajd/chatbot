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
