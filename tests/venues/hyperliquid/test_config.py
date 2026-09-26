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
