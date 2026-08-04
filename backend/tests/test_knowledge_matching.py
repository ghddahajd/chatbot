"""Regression tests for fuzzy KB matching."""

import shutil
from pathlib import Path

import yaml

from app.knowledge import KnowledgeBaseResolver, _token_prefix_match
from app.models import Message, MessageRole, PolicyAction
from app.policy import analyze_message
from app.policy.intent import classify_and_extract


def _copy_rosh_import_kb(resolver: KnowledgeBaseResolver, managed_env):
    source_dir = Path("backend/data/clients/rosh_import_demo")
    target_dir = managed_env["clients_dir"] / "rosh_import_demo"
    shutil.copytree(source_dir, target_dir)
    return resolver.get("rosh_import_demo", fallback=False)


def _set_article_map_excerpt(clients_dir: Path, url: str, excerpt: str | None) -> None:
    """Точечно ставит/убирает excerpt для одной записи по url — не зависит от того,
    есть ли у неё уже excerpt в реальных данных клиента (не хрупкий string-replace)."""

    map_path = clients_dir / "rosh_import_demo" / "article_service_map.yaml"
    payload = yaml.safe_load(map_path.read_text(encoding="utf-8"))
    normalized_url = url.rstrip("/")
    for item in payload.get("items", []):
        if str(item.get("url") or "").rstrip("/") == normalized_url:
            if excerpt is None:
                item.pop("excerpt", None)
            else:
                item["excerpt"] = excerpt
            break
    else:
        raise AssertionError(f"url not found in article_service_map.yaml: {url}")
    map_path.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")


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


def test_bioresonance_question_resolves_to_real_bicom_service_not_biorevit(
    policy_session, resolver, managed_env
) -> None:
    """Раньше "биорезонансная" ложно фаззи-матчилась на "Биоревитализация" (общий 5-символьный
    префикс "биоре") — эту коллизию не должно быть независимо от того, есть ли в данных
    реальная услуга BICOM. С тех пор как BICOM-услуги реально добавлены в rosh_import_demo,
    вопрос корректно резолвится в них (а не в defer, как было раньше при отсутствии данных)."""

    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    classification = _classify("в чем заключается биорезонансная диагностика", knowledge_base)

    result = analyze_message(
        "в чем заключается биорезонансная диагностика",
        policy_session,
        knowledge_base,
        classification,
    )

    assert result.action == PolicyAction.ANSWER
    assert result.service_id != "biorevitalizaciya_9d426f68"
    assert result.service_id in {
        "biorezonansnaya_terapiya_na_apparate_bicom_4d87fe07",
        "diagnostika_na_apparate_bicom_body_check_9bf8a621",
    }


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
    url = "https://www.medcenterrosh.ru/blog/vtoroi-podborodok-prichiny-poyavleniya-i-metody-korrekcii"
    _set_article_map_excerpt(managed_env["clients_dir"], url, "Одобренный короткий фрагмент статьи.")

    knowledge_base = resolver.get("rosh_import_demo", fallback=False)
    entry = knowledge_base.article_service_map[url]

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


def test_unknown_service_asks_followup_on_first_message(
    monkeypatch,
    policy_session,
    resolver,
    managed_env,
) -> None:
    """Живой баг (research.md #4, третий аудит): реальная классификация для симптомных
    сообщений вроде "выпадают волосы"/"тёмные круги" — unknown_service, не medical_advice.
    §3.2 скрипта — сначала уточняющий вопрос на первой реплике, не сразу услуга."""

    import app.policy as policy_module

    source_dir = Path("backend/data/clients/rosh_import_demo")
    target_dir = managed_env["clients_dir"] / "rosh_import_demo"
    shutil.copytree(source_dir, target_dir)
    _set_article_map_excerpt(
        managed_env["clients_dir"],
        "https://www.medcenterrosh.ru/problems/temnye-veki-i-krugi-pod-glazami",
        None,
    )
    knowledge_base = resolver.get("rosh_import_demo", fallback=False)
    monkeypatch.setattr(policy_module, "similar_services_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(policy_module, "_retrieve_article_context_safe", lambda message: [])

    result = analyze_message(
        "темные круги под глазами, что посоветуете?",
        policy_session,
        knowledge_base,
        {"intent": "unknown_service", "service_id": None, "confidence": 0.9},
    )

    assert result.action == PolicyAction.CLARIFY
    assert "Мезотерапия" not in result.safe_context["message_to_user"]


def test_unknown_service_uses_article_trigger_phrase_on_later_message(
    monkeypatch,
    policy_session,
    resolver,
    managed_env,
) -> None:
    import app.policy as policy_module

    source_dir = Path("backend/data/clients/rosh_import_demo")
    target_dir = managed_env["clients_dir"] / "rosh_import_demo"
    shutil.copytree(source_dir, target_dir)
    # тест намеренно проверяет поведение БЕЗ excerpt — не зависит от того, добавлен ли
    # он у этой записи в реальных данных клиента
    _set_article_map_excerpt(
        managed_env["clients_dir"],
        "https://www.medcenterrosh.ru/problems/temnye-veki-i-krugi-pod-glazami",
        None,
    )
    knowledge_base = resolver.get("rosh_import_demo", fallback=False)
    monkeypatch.setattr(policy_module, "similar_services_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(policy_module, "_retrieve_article_context_safe", lambda message: [])
    policy_session.messages.append(Message(role=MessageRole.ASSISTANT, text="Добрый день! Чем могу помочь?"))

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


def test_faq_question_prefers_approved_article_mapping_over_free_answer(
    monkeypatch,
    policy_session,
    resolver,
    managed_env,
) -> None:
    import app.policy as policy_module

    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    monkeypatch.setattr(policy_module, "_retrieve_article_context_safe", lambda message: [])

    result = analyze_message(
        "темные круги под глазами, что посоветуете?",
        policy_session,
        knowledge_base,
        {"intent": "faq_question", "service_id": None, "confidence": 0.9},
    )

    assert result.action == PolicyAction.ANSWER
    assert result.safe_context["question_type"] == "cosmetic_article_guidance"
    assert "Мезотерапия" in result.safe_context["message_to_user"]


def test_faq_question_without_approved_mapping_keeps_old_behavior(
    monkeypatch,
    policy_session,
    resolver,
    managed_env,
) -> None:
    import app.policy as policy_module

    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    monkeypatch.setattr(policy_module, "_retrieve_article_context_safe", lambda message: [])

    result = analyze_message(
        "что нельзя делать после чистки лица?",
        policy_session,
        knowledge_base,
        {"intent": "faq_question", "service_id": None, "confidence": 0.9},
    )

    assert result.action == PolicyAction.CLARIFY
    assert result.safe_context.get("question_type") != "cosmetic_article_guidance"


def test_regulated_without_hard_signal_asks_followup_on_first_message(
    monkeypatch,
    policy_session,
    resolver,
    managed_env,
) -> None:
    """§3.2 скрипта (research.md #4, третий аудит): на первой реплике с описанием СИМПТОМА
    (не названием услуги) бот сначала задаёт короткий уточняющий вопрос, не сразу предлагает
    услугу — canonical-пример из скрипта, ровно как в §3.2."""

    import app.policy as policy_module

    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    monkeypatch.setattr(policy_module, "_retrieve_article_context_safe", lambda message: [])

    result = analyze_message(
        "выпадают волосы, что можно сделать?",
        policy_session,
        knowledge_base,
        {"intent": "regulated_advice", "service_id": None, "confidence": 0.95},
    )

    assert result.action == PolicyAction.CLARIFY
    assert "Мезотерапия" not in result.safe_context["message_to_user"]


def test_regulated_without_hard_signal_uses_article_trigger_phrase_on_later_message(
    monkeypatch,
    policy_session,
    resolver,
    managed_env,
) -> None:
    """Тот же симптом, но НЕ на первой реплике сессии (бот уже раз ответил) — уточнять
    заново не нужно, curated-подсказка должна дойти до пользователя как раньше."""

    import app.policy as policy_module

    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    monkeypatch.setattr(policy_module, "_retrieve_article_context_safe", lambda message: [])
    policy_session.messages.append(Message(role=MessageRole.ASSISTANT, text="Добрый день! Чем могу помочь?"))

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


def test_curated_match_is_explicit_service_mention_helper(resolver, managed_env) -> None:
    """§3.3 скрипта: если совпавшая trigger_phrase — само название/синоним услуги (человек
    назвал услугу, не просто симптом), это НЕ должно гейтиться уточняющим вопросом даже на
    первой реплике. В реальных данных ROSH trigger_phrases всегда сформулированы как симптомы
    (так и задуман механизм), поэтому проверяем хелпер напрямую на синтетическом случае."""

    from app.models import PolicyResult, PolicyAction, PolicyReason
    from app.policy import _curated_match_is_explicit_service_mention

    knowledge_base = _copy_rosh_import_kb(resolver, managed_env)
    service = knowledge_base.services[0]

    explicit_result = PolicyResult(
        action=PolicyAction.ANSWER,
        reason=PolicyReason.OK,
        confidence=0.8,
        safe_context={
            "article_service_mapping": {
                "matched_phrase": service.name.lower(),
                "service_ids": [service.id],
            }
        },
    )
    assert _curated_match_is_explicit_service_mention(explicit_result, knowledge_base) is True

    symptom_result = PolicyResult(
        action=PolicyAction.ANSWER,
        reason=PolicyReason.OK,
        confidence=0.8,
        safe_context={
            "article_service_mapping": {
                "matched_phrase": "выпадают волосы",
                "service_ids": [service.id],
            }
        },
    )
    assert _curated_match_is_explicit_service_mention(symptom_result, knowledge_base) is False


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
