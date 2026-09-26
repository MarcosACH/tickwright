"""``HyperliquidConfig`` refuses a setting the live venue path cannot run on."""

import pytest
from pydantic import ValidationError

from tickwright.venues.hyperliquid import HyperliquidConfig


@pytest.mark.parametrize(
    "field", ["reconnect_initial_backoff_seconds", "reconnect_max_backoff_seconds"]
)
@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "1e300", "1e-10"])
def test_a_reconnect_backoff_that_is_not_a_usable_number_of_seconds_is_refused(
    field: str, value: str
) -> None:
    # Infinity and 1e300 load as floats, and the reconnect loop sleeps on them
    # forever. The feed then stops with no error while the engine reads RUNNING.
    # 1e-10 is under one nanosecond. The values are strings because that is how
    # they arrive from the environment.
    with pytest.raises(ValidationError, match=f"{field} must be a positive number"):
        HyperliquidConfig.model_validate({field: value})


def test_an_initial_backoff_larger_than_the_max_is_refused() -> None:
    # The max caps only the doubling. So the first reconnect would wait the
    # full initial 100 seconds, not the 10 the operator set as the most.
    with pytest.raises(
        ValidationError,
        match="reconnect_initial_backoff_seconds must be at most reconnect_max_backoff_seconds",
    ):
        HyperliquidConfig.model_validate(
            {"reconnect_initial_backoff_seconds": 100, "reconnect_max_backoff_seconds": 10}
        )


def test_an_initial_backoff_equal_to_the_max_loads() -> None:
    # A fixed delay with no doubling is a fair choice, so equal is allowed.
    config = HyperliquidConfig.model_validate(
        {"reconnect_initial_backoff_seconds": 10, "reconnect_max_backoff_seconds": 10}
    )
    assert config.reconnect_initial_backoff_seconds == 10
