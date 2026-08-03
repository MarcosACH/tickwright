"""Boundary economics: the signed fee a fill carries (ADR-0036) and the signed
funding a position accrues (ADR-0037).

Pure ``domain`` helpers, peers of ``quantize_size``/``below_min_notional``
(ADR-0017/0032), shared so the paper exchange computes what the live one reads in
the same shape. Each has its own sign convention, and each mirrors the venue
field it stands opposite: a fee is ``> 0`` cost debited, ``< 0`` maker rebate
credited — and making liquidity is *not* what makes a fee negative — while a
funding amount mirrors ``userFunding.usdc``, ``< 0`` paid and ``> 0`` received.
"""

from datetime import UTC, datetime
from decimal import Decimal

from tickwright.domain import InstrumentSpec, fill_fee, funding_amount, funding_boundaries

HOUR_NS = 3_600_000_000_000
"""The venue's real funding interval (ADR-0037), spelled out once for the cases below."""


def _at(text: str) -> int:
    """A UTC wall-clock instant as epoch ns — the tests' independent timeline.

    The boundary rule is stated in wall-clock terms ("the top of each UTC hour")
    and implemented in integer arithmetic off the epoch, so the cases below name
    the instants they mean and let ``datetime`` do the conversion. A case that
    spelled its own epoch integers would be re-deriving the thing under test.
    """
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp()) * 1_000_000_000


def _spec(*, maker_fee: str = "0", taker_fee: str = "0", funding_rate: str = "0") -> InstrumentSpec:
    return InstrumentSpec(
        symbol="BTC",
        sz_decimals=3,
        max_decimals=6,
        min_notional=Decimal("0"),
        maker_fee=Decimal(maker_fee),
        taker_fee=Decimal(taker_fee),
        funding_rate=Decimal(funding_rate),
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


def test_a_long_pays_funding_at_a_positive_rate() -> None:
    # A 2 BTC long at 50 000 is a 100 000 notional; an hourly rate of 0.01% is
    # 10 USDC over the boundary. Positive rate ⇒ longs pay shorts, so the amount
    # is *negative*: it mirrors ``userFunding.usdc``, where negative = paid
    # (ADR-0037, R2 §5).
    amount = funding_amount(
        signed_size=Decimal("2"), price=Decimal("50000"), spec=_spec(funding_rate="0.0001")
    )

    assert amount == Decimal("-10")


def test_a_short_receives_at_the_same_rate_the_long_pays() -> None:
    """The sign rides the position's direction, not a configured convention.

    The same 100 000 notional and the same positive rate as above, held the other
    way: the payment reverses to a credit of exactly what the long was debited.
    Asserting the pair together is what pins it — either one alone stays green if
    the leading minus were dropped and the rate's own sign carried the result.
    """
    long_amount = funding_amount(
        signed_size=Decimal("2"), price=Decimal("50000"), spec=_spec(funding_rate="0.0001")
    )
    short_amount = funding_amount(
        signed_size=Decimal("-2"), price=Decimal("50000"), spec=_spec(funding_rate="0.0001")
    )

    assert long_amount == Decimal("-10")  # < 0: paid
    assert short_amount == Decimal("10")  # > 0: received


def test_the_amount_reproduces_the_sign_and_magnitude_the_venue_reports() -> None:
    """The venue's own worked record, recomputed from our side (R2, ADR-0037).

    Hyperliquid's documented ``userFunding`` example holds ``szi = +49.1477``
    ETH at ``fundingRate = 0.0000417`` and reports ``usdc = -3.625312``. The
    oracle it was priced against is not in the record, so this asserts what the
    record can settle: at an ETH oracle around 1770 the amount lands on the
    venue's magnitude to the cent, with the venue's sign. That is the check
    worth having — the formula agreeing with a real payment rather than with
    itself.
    """
    amount = funding_amount(
        signed_size=Decimal("49.1477"), price=Decimal("1770"), spec=_spec(funding_rate="0.0000417")
    )

    assert amount < 0  # the long paid, exactly as the record's ``usdc`` did
    assert abs(amount - Decimal("-3.625312")) < Decimal("0.01")


def test_a_spec_that_declares_no_funding_rate_accrues_nothing() -> None:
    """The additive default, asserted on a spec built *without* the field.

    The frictionless path staying reachable (ADR-0037) — not a rate of zero
    someone configured — and the same guarantee ``maker_fee``/``taker_fee``
    carry: every construction site that predates funding still builds one of
    these and must keep accruing nothing.
    """
    frictionless = InstrumentSpec(
        symbol="BTC", sz_decimals=3, max_decimals=6, min_notional=Decimal("0")
    )

    amount = funding_amount(signed_size=Decimal("2"), price=Decimal("50000"), spec=frictionless)

    assert amount == Decimal("0")


def test_every_boundary_a_span_crosses_is_returned_in_order() -> None:
    """A jump across three boundaries yields three, not one (ADR-0037).

    Funding is **additive** where a reconcile cadence is convergent: each
    boundary is a distinct real payment, so a span that crosses several must
    enumerate them all. At the default one-hour interval that is the top of each
    UTC hour — the venue's real schedule — which the case names in wall-clock
    terms and the helper derives from the epoch.
    """
    crossed = funding_boundaries(
        after_ns=_at("2024-01-01 00:30:00"),
        through_ns=_at("2024-01-01 03:15:00"),
        interval_ns=HOUR_NS,
    )

    assert crossed == (
        _at("2024-01-01 01:00:00"),
        _at("2024-01-01 02:00:00"),
        _at("2024-01-01 03:00:00"),
    )


def test_consecutive_spans_settle_each_boundary_exactly_once() -> None:
    """The half-open span, asserted as the property it exists for.

    A generator settles up to ``t`` and passes ``t`` as the next span's
    ``after_ns``. If the span were closed at both ends, ``t`` would come back a
    second time and the boundary would be re-settled; if it were open at both,
    an instant landing exactly on a boundary would drop it and never return.
    Both ends are asserted here because either error alone reads as a plausible
    off-by-one somewhere else.
    """
    first = funding_boundaries(
        after_ns=_at("2024-01-01 00:30:00"),
        through_ns=_at("2024-01-01 02:00:00"),
        interval_ns=HOUR_NS,
    )
    second = funding_boundaries(
        after_ns=_at("2024-01-01 02:00:00"),
        through_ns=_at("2024-01-01 04:00:00"),
        interval_ns=HOUR_NS,
    )

    # ``through`` is inclusive: 02:00 is crossed by the first span, not deferred.
    assert first == (_at("2024-01-01 01:00:00"), _at("2024-01-01 02:00:00"))
    # ``after`` is exclusive: the second span resumes past it, never repeating it.
    assert second == (_at("2024-01-01 03:00:00"), _at("2024-01-01 04:00:00"))


def test_a_span_that_crosses_nothing_is_empty() -> None:
    """Two instants inside one interval have no boundary between them — the
    ordinary case between two ticks, and the one the generator must not treat as
    a payment. An empty tuple rather than a sentinel: there is nothing to settle,
    which is a real answer."""
    crossed = funding_boundaries(
        after_ns=_at("2024-01-01 00:10:00"),
        through_ns=_at("2024-01-01 00:50:00"),
        interval_ns=HOUR_NS,
    )

    assert crossed == ()
