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
