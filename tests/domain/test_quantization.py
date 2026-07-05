"""Quantization rules: the shared, pure sig-figs ∧ decimals quantizer (ADR-0017).

Size rounds **down** to ``sz_decimals`` (never exceed the strategy's intent);
price rounds toward the **passive side** (buy down, sell up) under the venue's
sig-figs ∧ decimals granularity rule. These are pure ``domain`` functions —
shared by the ``engine`` guard and every ``venues`` adapter (ADR-0032), so they
live where both can import them.
"""

from decimal import Decimal

from tickwright.domain import (
    InstrumentSpec,
    Side,
    below_min_notional,
    quantize_price,
    quantize_size,
)


def _spec(
    *,
    sz_decimals: int = 3,
    max_decimals: int = 6,
    max_sig_figs: int | None = None,
    min_notional: str = "0",
) -> InstrumentSpec:
    return InstrumentSpec(
        symbol="BTC",
        sz_decimals=sz_decimals,
        max_decimals=max_decimals,
        max_sig_figs=max_sig_figs,
        min_notional=Decimal(min_notional),
    )


def test_size_rounds_down_to_sz_decimals() -> None:
    # 1.23456 at sz_decimals=3 truncates to 1.234 — never rounds up past intent.
    assert quantize_size(Decimal("1.23456"), _spec(sz_decimals=3)) == Decimal("1.234")


def test_sub_tick_size_quantizes_to_zero() -> None:
    # 0.0004 below the 0.001 size tick rounds down to zero — the value the guard
    # reads as DENIED (a size that rounds to nothing must never be sent).
    assert quantize_size(Decimal("0.0004"), _spec(sz_decimals=3)) == Decimal("0")


def test_buy_price_rounds_down_to_the_passive_side() -> None:
    # A BUY's passive side is below the market: with 3 decimal places allowed
    # (max_decimals 6 − sz_decimals 3), 100.1237 rounds *down* to 100.123.
    price = quantize_price(Decimal("100.1237"), Side.BUY, _spec(sz_decimals=3, max_decimals=6))
    assert price == Decimal("100.123")


def test_sell_price_rounds_up_to_the_passive_side() -> None:
    # A SELL's passive side is above the market: 100.1231 rounds *up* to 100.124,
    # the opposite direction from a BUY at the same price.
    price = quantize_price(Decimal("100.1231"), Side.SELL, _spec(sz_decimals=3, max_decimals=6))
    assert price == Decimal("100.124")


def test_significant_figures_cap_tightens_the_grid_for_a_large_price() -> None:
    # With max_sig_figs=5, a five-integer-digit price has already spent its sig
    # figs, so no decimals are allowed even though the decimals rule alone would
    # permit four (max_decimals 6 − sz_decimals 2). A BUY at 42123.45 → 42123.
    spec = _spec(sz_decimals=2, max_decimals=6, max_sig_figs=5)
    assert quantize_price(Decimal("42123.45"), Side.BUY, spec) == Decimal("42123")


def test_integer_price_is_valid_beyond_the_significant_figure_cap() -> None:
    # Hyperliquid always allows integer prices regardless of sig figs: a six-digit
    # integer is left untouched, never rounded to the 5-sig-fig grid (42130).
    spec = _spec(sz_decimals=2, max_decimals=6, max_sig_figs=5)
    assert quantize_price(Decimal("421234"), Side.BUY, spec) == Decimal("421234")


def test_below_min_notional_when_the_notional_falls_short() -> None:
    # notional = 100 × 0.05 = 5, below min_notional 10 → below. The one home the
    # guard (LIMIT → DENIED) and every venue adapter (MARKET → REJECTED) share.
    assert below_min_notional(Decimal("100"), Decimal("0.05"), _spec(min_notional="10"))


def test_exactly_at_min_notional_is_not_below() -> None:
    # The boundary is strict: 100 × 0.1 = 10 equals min_notional 10, so it *clears*
    # — an order at exactly the minimum is allowed, never denied.
    assert not below_min_notional(Decimal("100"), Decimal("0.1"), _spec(min_notional="10"))


def test_above_min_notional_is_not_below() -> None:
    # notional = 100 × 0.2 = 20, above min_notional 10 → not below.
    assert not below_min_notional(Decimal("100"), Decimal("0.2"), _spec(min_notional="10"))
