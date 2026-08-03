"""Paper's funding generator: the boundary loop on the injected ``Clock`` (ADR-0037).

Paper has no venue to push funding, so it **generates** accruals — on
``Clock.sleep_until``, the same pure waiter the reconcile cadences use, and
deliberately *not* on ``run_cadence``: those collapse a time-jump to one firing
because reconciling is convergent, while funding is **additive** and a jump
across N boundaries is N distinct payments.

The seam is the ``Exchange``: a real ``PaperExchange`` over a real ``InMemoryBus``
and a ``ManualClock``, driven by the venue's own verbs. Nothing here sleeps —
virtual time is advanced by hand, which is the whole point of the primitive.
"""

import asyncio
from decimal import Decimal

from ledgers import GENESIS

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.domain import (
    AggressorSide,
    Event,
    FundingAccrual,
    InstrumentSpec,
    MarketTick,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
)

HOUR_NS = 3_600_000_000_000

# The instants below are named in wall-clock terms because the boundary rule is:
# an epoch-aligned multiple of the interval, which at one hour is the top of each
# UTC hour. 2024-01-01T00:00:00Z is 1 704 067 200 s since the epoch.
MIDNIGHT_NS = 1_704_067_200 * 1_000_000_000


def _at(hours: float) -> int:
    """``hours`` after 2024-01-01T00:00:00Z, in epoch ns."""
    return MIDNIGHT_NS + int(hours * HOUR_NS)


def _spec(funding_rate: str = "0") -> InstrumentSpec:
    return InstrumentSpec(
        symbol="BTC",
        sz_decimals=3,
        max_decimals=6,
        min_notional=Decimal("0"),
        funding_rate=Decimal(funding_rate),
    )


def _tick(price: str, ts: int, symbol: str = "BTC") -> MarketTick:
    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol=symbol,
        price=Decimal(price),
        size=Decimal("5"),
        aggressor_side=AggressorSide.BUY,
        trade_id=f"t{ts}",
        seq=0,
    )


def _market_order(*, side: Side = Side.BUY, qty: str = "2", cloid: str = "0xabc") -> PlaceOrder:
    return PlaceOrder(
        cloid=cloid,
        symbol="BTC",
        side=side,
        quantity=Decimal(qty),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def _venue(bus: InMemoryBus, clock: ManualClock, *, funding_rate: str = "0.0001") -> PaperExchange:
    return PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        instrument_specs={"BTC": _spec(funding_rate)},
    )


async def _quiesce(times: int = 5) -> None:
    """Let the generator task run to quiescence after virtual time moves."""
    for _ in range(times):
        await asyncio.sleep(0)


async def _drive(
    *,
    orders: tuple[PlaceOrder, ...] = (),
    through: float,
    funding_rate: str = "0.0001",
    start: float = 0.5,
) -> list[FundingAccrual]:
    """Open the venue at ``start``, fill ``orders`` at 50 000, then jump to ``through``.

    One driver for every case here, because the cases differ only in what was
    filled, at what rate, and how far time moved — and a boundary loop is
    testable *only* through those three, which is the point of the injected
    clock. The venue is started after the fills so every case begins from a
    settled position, exactly as a restart would.
    """
    bus = InMemoryBus()
    clock = ManualClock(start_ns=_at(start))
    accruals: list[FundingAccrual] = []

    async def collect(event: Event) -> None:
        assert isinstance(event, FundingAccrual)
        accruals.append(event)

    bus.subscribe(FundingAccrual, collect)
    venue = _venue(bus, clock, funding_rate=funding_rate)
    await bus.publish(_tick("50000", ts=_at(start)))
    for order in orders:
        await venue.place(order)

    await venue.start()
    await _quiesce()
    clock.advance_to(_at(through))
    await _quiesce()
    await venue.stop()
    return accruals


def test_a_jump_across_three_boundaries_accrues_three_separate_payments() -> None:
    """Funding is additive, so catch-up settles every boundary it crossed.

    A 2 BTC long at 50 000 at an hourly rate of 0.01% is 10 USDC per boundary.
    Advancing virtual time from 00:30 to 03:15 in one step crosses 01:00, 02:00
    and 03:00 — three distinct real payments. Collapsing them to one, as a
    convergent cadence would, silently under-charges by two hours of funding.
    """
    accruals = asyncio.run(_drive(orders=(_market_order(),), through=3.25))

    assert [accrual.boundary_ts_ns for accrual in accruals] == [_at(1), _at(2), _at(3)]
    assert [accrual.amount for accrual in accruals] == [Decimal("-10")] * 3


def test_neither_a_zero_rate_nor_a_flat_position_accrues_anything() -> None:
    """A zero amount emits **no event at all** — one rule covering both (ADR-0037).

    They are the same fact reached two ways: there is no payment to record, so
    there is nothing to key, nothing to make durable and no ledger churn on the
    default-``0`` path. Asserting them together is what pins it as one rule: a
    skip implemented only on the rate would leave a closed-out position paying
    funding on nothing, and one implemented only on the size would churn a keyed
    zero through the ledger for every frictionless spec.

    Both cases cross three boundaries, so neither is silent for want of time.
    """
    at_a_zero_rate = asyncio.run(_drive(orders=(_market_order(),), through=3.25, funding_rate="0"))
    flat_after_closing = asyncio.run(
        _drive(
            orders=(
                _market_order(),
                _market_order(side=Side.SELL, cloid="0xdef"),
            ),
            through=3.25,
        )
    )

    assert at_a_zero_rate == []
    assert flat_after_closing == []


def test_a_short_is_paid_over_the_same_boundary_a_long_would_pay() -> None:
    """The venue reads its own direction off the order, not off the fill.

    A ``FillReport`` carries a magnitude — direction lives on the saga's side —
    so the net size the accrual is computed from is folded where the venue still
    holds the order. Selling 2 BTC from flat is a short, and at the same positive
    rate the long paid 10 USDC for, the short is credited 10.
    """
    accruals = asyncio.run(_drive(orders=(_market_order(side=Side.SELL),), through=1.25))

    assert [accrual.amount for accrual in accruals] == [Decimal("10")]
