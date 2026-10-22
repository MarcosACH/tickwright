"""The wake interval rule both boot gates apply (ADR-0062)."""

import math

import pytest

from tickwright.domain import wake_interval_refusal


@pytest.mark.parametrize("interval", [None, 1, 1.0, 60, 3600.5])
def test_no_interval_and_one_second_or_more_are_fine(interval: object) -> None:
    assert wake_interval_refusal(interval) is None


@pytest.mark.parametrize(
    ("interval", "refusal"),
    [
        (0.5, "wake_interval_seconds must be at least 1 second, not 0.5"),
        (0, "wake_interval_seconds must be at least 1 second, not 0"),
        (-1, "wake_interval_seconds must be at least 1 second, not -1"),
    ],
)
def test_an_interval_under_one_second_is_refused(interval: float, refusal: str) -> None:
    assert wake_interval_refusal(interval) == refusal


@pytest.mark.parametrize(
    ("interval", "refusal"),
    [
        ("60", "wake_interval_seconds must be a finite number of seconds or None, not '60'"),
        # bool is an int to Python, so True would pass as 1 second.
        (True, "wake_interval_seconds must be a finite number of seconds or None, not True"),
        # NaN compares false to everything, so a bare "< 1" lets it through.
        (math.nan, "wake_interval_seconds must be a finite number of seconds or None, not nan"),
        # Infinity passes "< 1", and the strategy would never wake.
        (math.inf, "wake_interval_seconds must be a finite number of seconds or None, not inf"),
    ],
)
def test_anything_but_a_finite_number_is_refused(interval: object, refusal: str) -> None:
    assert wake_interval_refusal(interval) == refusal
