"""проверки domain-aware response validator."""

from app.validator import (
    fallback_after_invalid_response,
    validate_article_guidance_response,
    validate_consultation_response,
    validate_response,
)


def test_validator_blocks_wall_of_services_answer() -> None:
    """Живой репро (аудит §2026-08-22, "Ниже"): "чем вы занимаетесь" — модель сама (не
    шаблон) сгенерировала один абзац из 24 услуг через запятую вместо "1-3 предложения"
    (раздел 1 скрипта). Точный текст реального живого ответа."""

    context = {"question_type": "list_services", "user_message": "чем вы занимаетесь"}
    answer = (
        "Мы предлагаем биоревитализацию, ботулинотерапию, внутривенный лазер Шатл Комби, "
        "игольчатый RF лифтинг, консультации, лазерную терапию (Skin Tyte, Forever Clear, "
        "Forever Young, HALO), лазерную шлифовку, лазерную эпиляцию, лазерный пилинг, "
        "мезотерапию, пилинги, уходы и маски, филлеры, фотолечение BBL, чистки, эксимерный "
        "лазер, биорезонансную терапию на аппарате BICOM, гинекологию, диагностику на "
        "аппарате BICOM BODY CHECK, удаление новообразований, прессотерапию, экзосомальную "
        "терапию. Уточните, какая услуга Вас интересует?"
    )

    assert not validate_response(answer, context)


def test_validator_allows_short_service_mention_list() -> None:
    context = {"question_type": "list_services", "user_message": "чем вы занимаетесь"}
    answer = "У нас есть биоревитализация и мезотерапия. Уточните, что вас интересует?"

    assert validate_response(answer, context)


def test_validator_allows_wall_of_services_when_explicitly_requested() -> None:
    """Регрессия (пойман пользователем сразу после первого фикса): "покажи услуги"/"хочу
    услуги" — та же question_type=list_services, что и у "чем вы занимаетесь", но это ЯВНАЯ
    просьба показать список — полный ответ тут правильный, не "простыня". Лимит по запятым
    не должен применяться, если сообщение само явно просит список услуг."""

    answer = (
        "Мы предлагаем биоревитализацию, ботулинотерапию, лазерную эпиляцию, мезотерапию, "
        "пилинги, филлеры, чистки, консультации, гинекологию, прессотерапию, экзосомальную "
        "терапию. Уточните, какая услуга Вас интересует?"
    )

    for message in ["покажи услуги", "хочу услуги", "список услуг", "услуги"]:
        context = {"question_type": "list_services", "user_message": message}
        assert validate_response(answer, context), message


def test_validator_comma_limit_does_not_apply_outside_list_services() -> None:
    """Порог по запятым специфичен для list_services — обычный развёрнутый ответ на другой
    тип вопроса (например explanation) не должен внезапно начать отбрасываться."""

    context = {"question_type": "explanation"}
    answer = (
        "Процедура подходит для коррекции морщин, повышения тонуса кожи, увлажнения, "
        "стимуляции коллагена, выравнивания текстуры и общего омоложения."
    )

    assert validate_response(answer, context)


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


def test_article_guidance_validator_allows_disclosed_service_name_matching_equipment_pattern() -> None:
    """Живой репро (аудит §2026-08-22, поймано на Alice, но баг не модели): UNSUPPORTED_
    EQUIPMENT_PATTERNS — общий для всех клиентов список брендов ("bbl" среди них), написан не
    под конкретных клиентов. У rosh_import_demo реальная, раскрытая услуга «Фотолечение BBL» —
    любой ответ, который называет её по имени, раньше валился, независимо от того, кто его
    сгенерировал. Название услуги передаётся в service_names — должно быть освобождено от
    проверки, а не только от per-клиентского undisclosed_equipment_terms."""

    context = {
        "article_guidance_candidate": {
            "excerpt": "Повышенная активность сальных желез приводит к жирному блеску и расширенным порам.",
            "service_names": "Чистки, Фотолечение BBL, Консультации, Пилинги",
        }
    }

    assert validate_article_guidance_response(
        "При повышенной активности сальных желёз могут помочь чистки, фотолечение BBL, "
        "консультации или пилинги. Точный подбор процедуры подтвердит специалист на консультации.",
        context,
    )
    # но настоящую утечку (бренд НЕ входит в разрешённые названия) по-прежнему ловим
    assert not validate_article_guidance_response(
        "Рекомендую пройти чистку на аппарате Candela GentleLase, это поможет.",
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


def test_efficacy_claim_pattern_catches_audit_reported_phrasings() -> None:
    """Аудит поймал живые фразы, где старый паттерн (только 'эффективна при/от') давал
    ложноотрицательный результат: 'для' вместо 'при/от', и два обещания результата вообще без
    слова 'эффективн-'."""

    context = {
        "question_type": "faq_question",
        "article_context": [{"title": "Процедура", "snippet": "Нейтральное описание без обещаний."}],
    }

    for answer in (
        "Процедура эффективна для пациентов любого возраста.",
        "Показывает стабильные результаты при работе с проблемной кожей.",
        "Даёт стойкий результат уже после первого сеанса.",
    ):
        assert not validate_response(answer, context), answer


def test_efficacy_claim_pattern_catches_future_tense_phrasings() -> None:
    """research.md #6 (третий аудит): паттерн ловил "помогает от" (настоящее время), но не
    "поможет от"/"вылечит"/"избавит от" (будущее) — тот же класс необещанного результата,
    просто другая форма глагола."""

    context = {
        "question_type": "faq_question",
        "article_context": [{"title": "Процедура", "snippet": "Нейтральное описание без обещаний."}],
    }

    for answer in (
        "Процедура точно поможет от акне.",
        "Курс полностью вылечит акне за месяц.",
        "Уже после первого сеанса избавит от морщин.",
    ):
        assert not validate_response(answer, context), answer


def test_efficacy_claim_check_applies_to_price_duration_and_explanation_too() -> None:
    """Живой структурный баг (research.md #6, третий аудит): проверка на гарантийные фразы
    стояла только внутри question_type == "faq_question" — ответы про цену/длительность/
    объяснение услуги шли через тот же validate_response(), но защита не срабатывала."""

    answer = "Процедура точно поможет от акне и даёт стойкий результат."
    for question_type in ("price", "duration", "explanation", "faq_question"):
        assert not validate_response(answer, {"question_type": question_type}), question_type


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
