"""The one operator-authored input to the margin model (ADR-0040 §5, ADR-0044 §2).

Mode and leverage are **one value, not two maps**: ``updateLeverage {asset,
isCross, leverage}`` sets both in a single signed action, so a config able to
express "mode set, leverage unset" would invent a state the venue cannot hold.
"""

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from tickwright.domain import InstrumentSpec, LeverageSpec


def test_an_unconfigured_symbol_takes_the_safest_pair() -> None:
    """``1x`` isolated — full-notional collateral per position, minimal
    liquidation exposure (ADR-0040 §5).

    Leverage is off by default and opted into per symbol, so a symbol the
    operator never named is a *complete conservative specification* rather than
    a hole the model has to interpret.
    """
    spec = LeverageSpec()

    assert spec.mode == "isolated"
    assert spec.leverage == 1


def test_the_pair_is_frozen_so_a_mode_cannot_drift_from_its_leverage() -> None:
    """Rebinding half the pair is what "one value, not two maps" forbids.

    A resolved map is read by two consumers — the margin model and the venue
    adapter — and a mutable pair would let either of them re-point a mode while
    the leverage beside it stood, which is the disagreement ADR-0044 §2 rules
    out structurally rather than by convention.
    """
    spec = LeverageSpec(mode="cross", leverage=5)

    with pytest.raises(FrozenInstanceError):
        spec.mode = "isolated"  # type: ignore[misc]


def test_a_spec_declaring_no_cap_still_admits_the_default_leverage() -> None:
    """``max_leverage`` defaults to ``1``, **not** ``0`` (ADR-0040 §4).

    Zero would make ADR-0044 §9's ``1 ≤ leverage ≤ max_leverage`` unsatisfiable
    and fault every paper start on a default-valued spec — a bound nobody
    configured refusing a leverage nobody configured. ``1`` is the frictionless
    reading (a spec declaring no cap models no leverage), and the last assertion
    is the whole point: the two defaults this slice adds must satisfy the
    predicate the next behavior enforces.

    ``margin_maint`` takes the ``0`` its fee/funding neighbours take instead —
    frictionless maintenance — because it is a rate, not a bound.
    """
    spec = InstrumentSpec(symbol="BTC", sz_decimals=3, max_decimals=6, min_notional=Decimal("10"))

    assert spec.max_leverage == 1
    assert spec.margin_maint == Decimal("0")
    assert LeverageSpec().leverage <= spec.max_leverage
