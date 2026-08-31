"""The one operator-authored input to the margin model (ADR-0040 §5, ADR-0044 §2).

Mode and leverage are **one value, not two maps**: ``updateLeverage {asset,
isCross, leverage}`` sets both in a single signed action, so a config able to
express "mode set, leverage unset" would invent a state the venue cannot hold.
"""

from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from tickwright.domain import (
    DEFAULT_LEVERAGE,
    InstrumentSpec,
    LeverageBook,
    LeverageOutOfBounds,
    LeverageSpec,
)


def _spec(symbol: str, *, max_leverage: int) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol,
        sz_decimals=3,
        max_decimals=6,
        min_notional=Decimal("10"),
        max_leverage=max_leverage,
    )


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


def test_the_book_completes_the_sparse_config_over_the_traded_set() -> None:
    """The resolved value is a **type**, not a convention two call sites keep.

    ``AppConfig.leverage`` carries only the symbols the operator named; what
    both consumers must receive is one entry per strategy-traded symbol
    (ADR-0044 §3), so the completion is the book's only constructor rather than
    a comprehension the composition root remembers to run.
    """
    book = LeverageBook.resolve(
        {"ETH": LeverageSpec(mode="cross", leverage=10)}, traded=["BTC", "ETH"]
    )

    assert book.entries == {
        "BTC": LeverageSpec(mode="isolated", leverage=1),
        "ETH": LeverageSpec(mode="cross", leverage=10),
    }


def test_a_symbol_outside_the_book_reads_the_default_the_resolution_would_have_filled() -> None:
    """One fallback, in one place.

    The resolution's default for an unconfigured traded symbol and the read's
    default for a symbol the book does not carry are the *same* answer — so a
    read can never be more permissive than the map, and the two cannot drift
    apart by living in two modules.
    """
    book = LeverageBook.resolve({}, traded=["BTC"])

    assert book.for_symbol("BTC") == DEFAULT_LEVERAGE
    assert book.for_symbol("SOL") == DEFAULT_LEVERAGE


def test_a_traded_symbol_with_no_instrument_spec_is_refused_by_its_missing_spec() -> None:
    """ADR-0044 §9's first clause: *every* strategy-traded symbol must have an
    ``InstrumentSpec``.

    The refusal is diagnosed as the missing **spec** it is, not as a leverage
    the operator never configured: this book carries only the ``1x`` default,
    so an error naming the leverage would send an operator to
    ``TICKWRIGHT_LEVERAGE`` for a hole in the exchange's instrument universe.
    """
    book = LeverageBook.resolve({}, traded=["BTC"])

    with pytest.raises(LeverageOutOfBounds) as refusal:
        book.validate_against({})

    assert "BTC" in str(refusal.value)
    assert "InstrumentSpec" in str(refusal.value)


def test_a_leverage_below_one_is_refused_like_one_above_the_cap() -> None:
    """``1 ≤ leverage ≤ max_leverage`` is a bound at **both** ends (ADR-0044 §9).

    ``0x`` is not "no leverage" — it is a zero denominator in every margin the
    model computes, so it is refused at the same startup moment as an
    over-levered symbol rather than surfacing as an arithmetic failure the
    first time a position is valued.
    """
    book = LeverageBook.resolve({"BTC": LeverageSpec(leverage=0)}, traded=["BTC"])

    with pytest.raises(LeverageOutOfBounds) as refusal:
        book.validate_against({"BTC": _spec("BTC", max_leverage=40)})

    assert "BTC" in str(refusal.value)


def test_every_offending_symbol_is_named_on_one_start() -> None:
    """An operator who broke two symbols learns both on the first boot.

    Both *kinds* of offence are collected on the one pass, so a run with an
    unbounded symbol and an over-levered one does not report them one restart
    at a time — the promise ``LeverageOutOfBounds`` states and only the
    ``AppConfig`` dead-entry validator was pinned on.
    """
    book = LeverageBook.resolve(
        {"BTC": LeverageSpec(leverage=50), "SOL": LeverageSpec(leverage=3)},
        traded=["BTC", "ETH", "SOL"],
    )

    with pytest.raises(LeverageOutOfBounds) as refusal:
        book.validate_against(
            {"BTC": _spec("BTC", max_leverage=40), "SOL": _spec("SOL", max_leverage=20)}
        )

    message = str(refusal.value)
    assert "BTC" in message
    assert "ETH" in message
    assert "SOL" not in message
