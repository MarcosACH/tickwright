"""Fill-boundary economics: the signed fee a fill carries (ADR-0036).

A pure ``domain`` helper, peer of ``quantize_size``/``below_min_notional``
(ADR-0017/0032), shared so the paper exchange computes what the live one reads in
the same shape. The sign convention is ``> 0`` cost debited, ``< 0`` maker rebate
credited — and making liquidity is *not* what makes a fee negative.
"""

from decimal import Decimal

from tickwright.domain import InstrumentSpec, fill_fee


def _spec(*, maker_fee: str = "0", taker_fee: str = "0") -> InstrumentSpec:
    return InstrumentSpec(
        symbol="BTC",
        sz_decimals=3,
        max_decimals=6,
        min_notional=Decimal("0"),
        maker_fee=Decimal(maker_fee),
        taker_fee=Decimal(taker_fee),
    )


def test_a_taker_fill_charges_the_taker_rate_on_notional() -> None:
    # Hyperliquid's base taker rate, 0.045%, against a 0.5 BTC fill at 50 000:
    # a 25 000 notional charged 11.25 USDC. A cost, so positive.
    fee = fill_fee(
        price=Decimal("50000"),
        quantity=Decimal("0.5"),
        maker=False,
        spec=_spec(taker_fee="0.00045"),
    )

    assert fee == Decimal("11.25")
