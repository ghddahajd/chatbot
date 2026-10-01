"""датчики сигналов: примеры «должно сработать / не должно» каждого датчика — прямо из detectors.py."""

from __future__ import annotations

import pytest

from app.knowledge import normalize_text
from app.policy import detectors
from app.policy.detectors import DETECTORS, Detector

CASES = [
    pytest.param(detector, example, expected, id=f"{detector.name}:{'да' if expected else 'нет'}:{example}")
    for detector in DETECTORS
    for expected, examples in ((True, detector.examples_yes), (False, detector.examples_no))
    for example in examples
]


@pytest.mark.parametrize(("detector", "example", "expected"), CASES)
def test_detector_examples(detector: Detector, example: str, expected: bool) -> None:
    assert detector(normalize_text(example)) is expected


def test_every_detector_has_examples_both_ways() -> None:
    for detector in DETECTORS:
        assert detector.examples_yes, detector.name
        assert detector.examples_no, detector.name


def test_every_detector_is_registered() -> None:
    # незарегистрированный датчик выпал бы и из проверки примеров, и из корпуса сети безопасности
    defined = {value.name for value in vars(detectors).values() if isinstance(value, Detector)}
    assert defined == {detector.name for detector in DETECTORS}
    assert len(DETECTORS) == len(defined)
