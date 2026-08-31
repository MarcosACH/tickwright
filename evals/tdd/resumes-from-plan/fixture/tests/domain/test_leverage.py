import pytest
from domain.leverage import InstrumentSpec, LeverageSetting, check_leverage


def test_leverage_below_one_is_refused() -> None:
    with pytest.raises(ValueError):
        check_leverage(LeverageSetting(leverage=0), InstrumentSpec("BTC", max_leverage=50))
