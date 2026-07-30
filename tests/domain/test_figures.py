"""``exact_figure`` — the one guard against a figure that is not a number.

A peer of ``test_quantization.py``: a pure ``domain`` function, tested directly
as well as through the three boundaries that call it (the Hyperliquid feed and
fills readers, the replay feed), because it is what those three agree on.
"""

from decimal import Decimal, InvalidOperation

import pytest

from tickwright.domain import exact_figure


@pytest.mark.parametrize("reported", ["NaN", "-NaN", "sNaN", "Infinity", "-Infinity"])
def test_a_figure_that_is_not_a_number_is_refused(reported: str) -> None:
    # Every one of these is a *valid* Decimal construction — that is the whole
    # problem. None raises on the way in, so the refusal has to be explicit.
    figure = Decimal(reported)

    with pytest.raises(ValueError):
        exact_figure(figure)


def test_the_refused_figures_are_the_ones_no_downstream_check_can_catch() -> None:
    """Why refusal at the boundary, and not merely strictness — pinning what
    ``Decimal`` actually does, which is *not* the ``float`` rule. Ordering
    against a ``NaN`` signals rather than returning false, so it does not clear a
    band check quietly; it detonates one, arbitrarily far downstream. Equality
    and arithmetic are the silent ones, and an infinity clears any ceiling."""
    nan = Decimal("NaN")

    # Not fail-open, fail-*late*: this is the exception a guard's comparison
    # raises deep in the engine, layers away from the read that admitted it.
    with pytest.raises(InvalidOperation):
        _ = nan < Decimal("10")

    # These are the silent ones. A cum_qty cross-check never agrees, so an order
    # never reaches FILLED; and one bad fill poisons every total derived from it.
    assert (nan == nan) is False
    assert (nan + Decimal("5")).is_nan()
    # Durable, too: the store writes str() and reads Decimal() back.
    assert Decimal(str(nan)).is_nan()
    # The infinity needs no subtlety — it simply clears whatever ceiling it meets.
    assert Decimal("Infinity") > Decimal("1e12")

    for unreadable in (nan, Decimal("Infinity")):
        with pytest.raises(ValueError):
            exact_figure(unreadable)


def test_an_exact_figure_passes_through_with_its_recorded_scale_intact() -> None:
    # The guard is a check, not a normalizer: a figure reported to two decimal
    # places comes back at two decimal places. Anything else would silently
    # re-scale venue quantities on their way into the ledger (ADR-0029).
    assert str(exact_figure(Decimal("0.10"))) == "0.10"
    assert str(exact_figure(Decimal("43250"))) == "43250"
    assert exact_figure(Decimal("-3.5")) == Decimal("-3.5")
    assert exact_figure(Decimal("0")) == Decimal("0")
