"""``VenueAccountState``: the checks a venue account read makes on itself."""

from decimal import Decimal

import pytest

from tickwright.domain import InvariantViolation, VenueAccountState


def _state(cash_quantum: str) -> VenueAccountState:
    return VenueAccountState(
        equity=Decimal("100"),
        free_margin=Decimal("100"),
        cross_maintenance_margin=Decimal("0"),
        as_of_ts_ns=0,
        cash_quantum=Decimal(cash_quantum),
    )


@pytest.mark.parametrize("unit", ["0.000001", "0.01", "1"])
def test_a_power_of_ten_is_a_cash_unit(unit: str) -> None:
    assert _state(unit).cash_quantum == Decimal(unit)


@pytest.mark.parametrize("unit", ["0", "-0.000001", "0.000002", "0.0000010", "1E+2", "NaN"])
def test_a_cash_unit_that_is_not_a_small_power_of_ten_is_refused(unit: str) -> None:
    """The reconcile rounds both cash lines to this unit (#580). A wrong unit
    must fail loud. ``0`` would round to whole USDC and hide a 0.4 gap, and
    ``1E+2`` would hide anything under 50. Neither would raise on its own.
    """
    with pytest.raises(InvariantViolation, match="cash_quantum"):
        _state(unit)
