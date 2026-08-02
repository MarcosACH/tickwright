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


def test_a_maker_fill_charges_the_maker_rate_on_the_same_notional() -> None:
    # The same 25 000 notional as above, so the *rate selection* is the only
    # thing that differs: Hyperliquid's base maker rate, 0.015%, is 3.75 USDC
    # against the taker's 11.25. This is the whole of what ``maker`` decides.
    fee = fill_fee(
        price=Decimal("50000"),
        quantity=Decimal("0.5"),
        maker=True,
        spec=_spec(maker_fee="0.00015", taker_fee="0.00045"),
    )

    assert fee == Decimal("3.75")


def test_making_liquidity_is_not_what_makes_a_fee_negative() -> None:
    """The sign rides the configured rate, never the maker/taker bit (ADR-0036).

    A rebate is a property of the account's 14-day volume tier, so on a fresh
    account a maker fill is a positive cost like any other — observed on testnet
    in #152. Both calls below are makers; only the *rate* differs, and only the
    second is a credit. Asserting them together is what pins the claim: either
    one alone stays green if ``maker`` were ever made to imply a sign.
    """
    spec_at_base_rates = _spec(maker_fee="0.00015")
    spec_at_a_rebate_tier = _spec(maker_fee="-0.00003")

    cost = fill_fee(
        price=Decimal("50000"), quantity=Decimal("0.5"), maker=True, spec=spec_at_base_rates
    )
    rebate = fill_fee(
        price=Decimal("50000"), quantity=Decimal("0.5"), maker=True, spec=spec_at_a_rebate_tier
    )

    assert cost == Decimal("3.75")  # > 0: debited
    assert rebate == Decimal("-0.75")  # < 0: credited


def test_a_spec_that_declares_no_rates_charges_nothing_on_either_side() -> None:
    """The additive default, asserted on a spec built *without* the fee fields.

    Every construction site that predates fees still builds one of these, so
    this is the frictionless path staying reachable (ADR-0036) — not a rate of
    zero someone configured. Both branches, because a default that only held on
    one would leave the other charging against a field nobody set.
    """
    frictionless = InstrumentSpec(
        symbol="BTC", sz_decimals=3, max_decimals=6, min_notional=Decimal("0")
    )

    for maker in (True, False):
        assert fill_fee(
            price=Decimal("50000"), quantity=Decimal("0.5"), maker=maker, spec=frictionless
        ) == Decimal("0")


def test_the_computed_fee_keeps_every_digit_the_rate_produces() -> None:
    """Unrounded on purpose: paper's fee is exact, not truncated to the venue's
    reporting precision (ADR-0029/0036).

    The venue reports its own fee at 6 dp, but that is *its* precision on a
    number it is the authority for, and the live path reads that number verbatim
    rather than through here. 0.002 @ 65 239 at 0.045% is 0.0587151 — nine
    decimal places of exact product, which a 6-dp quantize would silently shave
    to 0.058715.
    """
    fee = fill_fee(
        price=Decimal("65239.0"),
        quantity=Decimal("0.002"),
        maker=False,
        spec=_spec(taker_fee="0.00045"),
    )

    assert fee == Decimal("0.0587151")
