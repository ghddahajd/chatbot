"""проверки domain-aware response validator."""

from app.validator import (
    fallback_after_invalid_response,
    validate_article_guidance_response,
    validate_consultation_response,
    validate_response,
)


def test_consultation_validator_blocks_medical_terms_for_medical_profile() -> None:
    context = {"domain_profile": {"type": "medical", "restricted_advice": ["medical_treatment"]}}

    assert not validate_consultation_response("Это безопасно и не опасно после процедуры.", context)
    assert not validate_consultation_response("Рубца обычно не будет после удаления.", context)


def test_consultation_validator_allows_same_words_for_auto_profile() -> None:
    context = {"domain_profile": {"type": "auto_service", "restricted_advice": ["remote_safety_assessment"]}}

    assert validate_consultation_response("Безопасность автомобиля лучше проверить на диагностике.", context)


def test_consultation_validator_blocks_raw_context_for_any_profile() -> None:
    context = {"domain_profile": {"type": "auto_service", "restricted_advice": []}}

    assert not validate_consultation_response("service_id: oil_change", context)


def test_consultation_validator_blocks_unsupported_equipment_brands() -> None:
    context = {"domain_profile": {"type": "medical", "restricted_advice": ["medical_treatment"]}}

    assert not validate_consultation_response(
        "Для эпиляции обычно используют Nd:YAG или Alexandrite лазеры.",
        context,
    )


def test_faq_validator_allows_answer_grounded_in_article_context() -> None:
    context = {
        "question_type": "faq_question",
        "article_context": [
            {
                "title": "Кольпоскопия",
                "snippet": "Кольпоскопия помогает врачу осмотреть шейку матки и выявить изменения тканей.",
            }
        ],
    }

    assert validate_response("Кольпоскопия помогает осмотреть шейку матки и выявить изменения тканей.", context)


def test_article_guidance_validator_allows_grounded_soft_phrase() -> None:
    context = {
        "article_guidance_candidate": {
            "excerpt": "Методы коррекции подбираются индивидуально после консультации.",
            "service_names": "Мезотерапия, Биоревитализация",
        }
    }

    assert validate_article_guidance_response(
        (
            "В материалах центра указано, что методы коррекции подбираются индивидуально после консультации. "
            "С этой темой связаны Мезотерапия и Биоревитализация. "
            "Точный подбор подтвердит специалист на консультации."
        ),
        context,
    )


def test_article_guidance_validator_blocks_recommendation_language() -> None:
    context = {
        "article_guidance_candidate": {
            "excerpt": "Методы коррекции подбираются индивидуально после консультации.",
            "service_names": "Филлеры",
        }
    }

    assert not validate_article_guidance_response(
        "Рекомендую вам Филлеры, это вам подходит.",
        context,
    )


def test_faq_validator_allows_non_price_numbers_grounded_in_article_context() -> None:
    context = {
        "question_type": "faq_question",
        "article_context": [
            {
                "title": "Уход после шлифовки",
                "snippet": "После процедуры важно использовать SPF 30 и избегать активного ухода 24 часа.",
            }
        ],
    }

    assert validate_response("После процедуры важно использовать SPF 30 и избегать активного ухода 24 часа.", context)


def test_faq_validator_blocks_prices() -> None:
    context = {
        "question_type": "faq_question",
        "article_context": [{"title": "Кольпоскопия", "snippet": "Кольпоскопия помогает врачу."}],
    }

    assert not validate_response("Кольпоскопия стоит 5000 рублей.", context)


def test_faq_validator_blocks_ungrounded_answer() -> None:
    context = {
        "question_type": "faq_question",
        "article_context": [{"title": "Кольпоскопия", "snippet": "Кольпоскопия помогает врачу."}],
    }

    assert not validate_response("После процедуры можно гарантировать быстрый результат.", context)


def test_faq_validator_blocks_ungrounded_equipment_brand() -> None:
    """regression: живой прогон дал 'диодных системах Palo' для лазерной эпиляции,
    хотя в источнике бренд не упоминался — та же категория бага, что и Nd:YAG/
    Alexandrite в consultation-пути, только в faq_question/RAG-пути."""

    context = {
        "question_type": "faq_question",
        "article_context": [
            {"title": "Лазерная эпиляция бикини", "snippet": "Лазерная эпиляция воздействует на волосяной фолликул."}
        ],
    }

    assert not validate_response(
        "Мужская лазерная эпиляция выполняется на эффективных диодных системах Palomar Vectus.",
        context,
    )


def test_faq_validator_blocks_ungrounded_toxin_name_and_falls_back_to_article() -> None:
    context = {
        "question_type": "faq_question",
        "user_message": "чем отличается ксеомин от миотокса",
        "article_context": [
            {
                "title": "Ксеомин в косметологии",
                "snippet": (
                    "Ксеомин отличается от других препаратов на основе ботулотоксина "
                    "отсутствием комплексообразующих белков."
                ),
            }
        ],
    }

    assert not validate_response(
        "Ксеомин отличается от Микротокса отсутствием комплексообразующих белков.",
        context,
    )
    assert fallback_after_invalid_response("Ксеомин отличается от Микротокса.", context).startswith(
        "По данным статьи «Ксеомин в косметологии»: Ксеомин отличается"
    )


def test_faq_validator_blocks_equipment_brand_even_when_grounded_in_source() -> None:
    context = {
        "question_type": "faq_question",
        "article_context": [
            {
                "title": "Лазерная эпиляция",
                "snippet": "Используется аппарат Sciton Joule и BBL Forever Clear.",
            }
        ],
    }

    assert not validate_response(
        "Используется аппарат Sciton Joule и BBL Forever Clear.",
        context,
    )


def test_faq_validator_blocks_medical_efficacy_claim_even_when_grounded() -> None:
    context = {
        "question_type": "faq_question",
        "article_context": [{"title": "Лазер", "snippet": "Эксимерный лазер разрушает бактерии."}],
    }

    assert not validate_response("Эксимерный лазер разрушает бактерии.", context)


def test_faq_validator_blocks_client_specific_undisclosed_equipment_name() -> None:
    """B6: захардкоженный UNSUPPORTED_EQUIPMENT_PATTERNS не знает реальные бренды конкретного
    клиента (например ROSH-овский InMode Morpheus8) — per-клиентский список из
    undisclosed_equipment_terms должен ловить их отдельно, даже если источник его упоминает."""

    context = {
        "question_type": "faq_question",
        "article_context": [
            {"title": "RF-лифтинг", "snippet": "Используется аппарат InMode Morpheus8."}
        ],
        "undisclosed_equipment_terms": ["InMode Morpheus8", "морфеус", "инмод"],
    }

    assert not validate_response("Используется аппарат InMode Morpheus8.", context)
    assert not validate_response("Мы используем Морфеус для лифтинга.", context)


def test_faq_validator_allows_answer_when_equipment_is_disclosed() -> None:
    """Если клиент раскрыл бренд (disclose: true), undisclosed_equipment_terms для этой записи
    пуст — валидатор не должен блокировать упоминание бренда, которого нет в списке."""

    context = {
        "question_type": "faq_question",
        "article_context": [
            {"title": "RF-лифтинг", "snippet": "Используется аппарат InMode Morpheus8."}
        ],
        "undisclosed_equipment_terms": [],
    }

    assert validate_response("Используется аппарат InMode Morpheus8.", context)


def test_faq_validator_no_longer_unlocked_by_user_mentioning_brand_first() -> None:
    """B6: раньше grounding_text для brand-проверки включал user_message — пользователь мог
    сам назвать бренд в вопросе и тем самым 'разблокировать' его повтор в ответе модели.
    Источник (article_context) бренд не упоминает — только пользователь."""

    context = {
        "question_type": "faq_question",
        "user_message": "у вас есть аппарат Мегасвет?",
        "article_context": [{"title": "RF-лифтинг", "snippet": "Обычный текст без брендов."}],
    }

    assert not validate_response("Да, используется аппарат Мегасвет для процедуры.", context)


def test_consultation_validator_blocks_client_specific_undisclosed_equipment_name() -> None:
    context = {
        "domain_profile": {"type": "medical", "restricted_advice": ["medical_treatment"]},
        "undisclosed_equipment_terms": ["InMode Morpheus8"],
    }

    assert not validate_consultation_response(
        "Для лифтинга используется InMode Morpheus8.",
        context,
    )


def test_article_guidance_validator_blocks_client_specific_undisclosed_equipment_name() -> None:
    context = {
        "article_guidance_candidate": {
            "excerpt": "Используется аппарат InMode Morpheus8 для RF-лифтинга.",
            "service_names": "Игольчатый RF-лифтинг",
        },
        "undisclosed_equipment_terms": ["InMode Morpheus8"],
    }

    assert not validate_article_guidance_response(
        "В материалах центра указано, что используется аппарат InMode Morpheus8.",
        context,
    )
