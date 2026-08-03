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


async def _settle(times: int = 5) -> None:
    """Let the generator task run to quiescence after virtual time moves."""
    for _ in range(times):
        await asyncio.sleep(0)


def test_a_jump_across_three_boundaries_accrues_three_separate_payments() -> None:
    """Funding is additive, so catch-up settles every boundary it crossed.

    A 2 BTC long at 50 000 at an hourly rate of 0.01% is 10 USDC per boundary.
    Advancing virtual time from 00:30 to 03:15 in one step crosses 01:00, 02:00
    and 03:00 — three distinct real payments. Collapsing them to one, as a
    convergent cadence would, silently under-charges by two hours of funding.
    """

    async def main() -> list[FundingAccrual]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=_at(0.5))
        accruals: list[FundingAccrual] = []

        async def collect(event: Event) -> None:
            assert isinstance(event, FundingAccrual)
            accruals.append(event)

        bus.subscribe(FundingAccrual, collect)
        venue = _venue(bus, clock)
        await bus.publish(_tick("50000", ts=_at(0.5)))
        await venue.place(_market_order())

        await venue.start()
        await _settle()
        clock.advance_to(_at(3.25))
        await _settle()
        await venue.stop()
        return accruals

    accruals = asyncio.run(main())

    assert [accrual.boundary_ts_ns for accrual in accruals] == [_at(1), _at(2), _at(3)]
    assert [accrual.amount for accrual in accruals] == [Decimal("-10")] * 3
