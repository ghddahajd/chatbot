"""проверки вспомогательной логики chat route."""

from app.routes.chat_utils import should_ignore_model_location_mismatch


def test_ignore_model_location_mismatch_when_user_is_in_company_city() -> None:
    assert should_ignore_model_location_mismatch(
        "я из района динамо в москве норм?",
        "Москва",
        {"intent": "service_mention", "service_id": None, "confidence": 0.0},
        {"intent": "location_mismatch", "service_id": None, "confidence": 0.91},
    )


def test_keep_model_location_mismatch_for_nearby_city() -> None:
    assert not should_ignore_model_location_mismatch(
        "а из химок?",
        "Москва",
        {"intent": "service_mention", "service_id": None, "confidence": 0.0},
        {"intent": "location_mismatch", "service_id": None, "confidence": 0.91},
    )
