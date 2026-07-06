"""проверки domain-aware response validator."""

from app.validator import validate_consultation_response, validate_response


def test_consultation_validator_blocks_medical_terms_for_medical_profile() -> None:
    context = {"domain_profile": {"type": "medical", "restricted_advice": ["medical_treatment"]}}

    assert not validate_consultation_response("Это безопасно и не опасно после процедуры.", context)


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
