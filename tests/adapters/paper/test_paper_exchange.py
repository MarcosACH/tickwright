"""``PaperExchange`` + ``ImmediateFillModel`` (ADR-0012 / ADR-0027).

The paper exchange caches the latest tick per symbol and fills a MARKET order on
receipt against that tick's price, emitting a raw ``FillReport`` on the bus (the
exchange owns no saga — ADR-0015). The default fill model is deterministic,
optimistic, zero-slippage, full-fill: no RNG at all.
"""

import asyncio
from decimal import Decimal

import pytest

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.domain import (
    AggressorSide,
    FillReport,
    MarketTick,
    OrderState,
    OrderStatusReport,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
)


def _tick(price: str, ts: int = 1_000, symbol: str = "BTC") -> MarketTick:
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


def _market_order(qty: str = "1", symbol: str = "BTC", cloid: str = "0xabc") -> PlaceOrder:
    return PlaceOrder(
        cloid=cloid,
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal(qty),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def _limit_order(
    price: str,
    *,
    time_in_force: TimeInForce = TimeInForce.GTC,
    post_only: bool = False,
    side: Side = Side.BUY,
    qty: str = "1",
    symbol: str = "BTC",
    cloid: str = "0xabc",
) -> PlaceOrder:
    return PlaceOrder(
        cloid=cloid,
        symbol=symbol,
        side=side,
        quantity=Decimal(qty),
        order_type=OrderType.LIMIT,
        time_in_force=time_in_force,
        price=Decimal(price),
        post_only=post_only,
    )


def _harness() -> tuple[PaperExchange, InMemoryBus, ManualClock, list[FillReport]]:
    bus = InMemoryBus()
    clock = ManualClock()
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
    bus.subscribe(MarketTick, exchange.on_tick)
    reports: list[FillReport] = []
    bus.subscribe(FillReport, lambda r: _record(reports, r))
    return exchange, bus, clock, reports


def _limit_harness() -> tuple[
    PaperExchange, InMemoryBus, ManualClock, list[FillReport], list[OrderStatusReport]
]:
    exchange, bus, clock, fills = _harness()
    statuses: list[OrderStatusReport] = []
    bus.subscribe(OrderStatusReport, lambda r: _record(statuses, r))
    return exchange, bus, clock, fills, statuses


def test_market_order_fills_at_the_latest_tick_price() -> None:
    exchange, bus, clock, reports = _harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_market_order(qty="0.5"))

    asyncio.run(scenario())

    assert len(reports) == 1
    report = reports[0]
    assert report.cloid == "0xabc"
    assert report.price == Decimal("42000")
    assert report.quantity == Decimal("0.5")  # full fill, no partial
    assert report.symbol == "BTC"


def test_market_order_uses_the_most_recent_tick() -> None:
    exchange, bus, clock, reports = _harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("100", ts=1_000))
        clock.advance_to(2_000)
        await bus.publish(_tick("101", ts=2_000))
        await exchange.place(_market_order())

    asyncio.run(scenario())

    assert reports[0].price == Decimal("101")


def test_fill_report_is_stamped_from_the_clock() -> None:
    exchange, bus, clock, reports = _harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("100"))
        clock.advance_to(7_500)
        await exchange.place(_market_order())

    asyncio.run(scenario())

    assert reports[0].ts_event == 7_500
    assert reports[0].ts_init == 7_500


def test_fill_trade_id_is_deterministic_across_runs() -> None:
    def run() -> str:
        exchange, bus, clock, reports = _harness()

        async def scenario() -> None:
            clock.advance_to(1_000)
            await bus.publish(_tick("100"))
            await exchange.place(_market_order())

        asyncio.run(scenario())
        return reports[0].trade_id

    assert run() == run()


def test_market_order_without_a_cached_tick_is_rejected() -> None:
    exchange, _, _, _ = _harness()
    # No tick has been published, so there is no price to fill against.
    with pytest.raises(ValueError, match="no market tick"):
        asyncio.run(exchange.place(_market_order()))


def test_gtc_limit_that_does_not_cross_rests_without_filling() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # A BUY LIMIT at 41000 is below the market (42000): it cannot fill now, so
        # it rests on the book and the venue reports it working (LIVE).
        await exchange.place(_limit_order("41000"))

    asyncio.run(scenario())

    assert fills == []
    assert len(statuses) == 1
    assert statuses[0].cloid == "0xabc"
    assert statuses[0].status is OrderState.LIVE


def test_resting_limit_fills_when_a_later_tick_crosses_its_price() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_limit_order("41000"))  # rests below the market
        # The market falls to the limit: the resting BUY now crosses and fills.
        clock.advance_to(2_000)
        await bus.publish(_tick("41000", ts=2_000))

    asyncio.run(scenario())

    assert len(fills) == 1
    assert fills[0].cloid == "0xabc"
    assert fills[0].quantity == Decimal("1")
    assert fills[0].price == Decimal("41000")  # filled at its limit price


def test_resting_sell_limit_fills_when_a_later_tick_rises_to_its_price() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # A SELL LIMIT at 43000 is above the market (42000): it cannot fill now, so
        # it rests. When the market rises to the limit the resting SELL crosses.
        await exchange.place(_limit_order("43000", side=Side.SELL))
        clock.advance_to(2_000)
        await bus.publish(_tick("43000", ts=2_000))

    asyncio.run(scenario())

    assert [s.status for s in statuses] == [OrderState.LIVE]
    assert len(fills) == 1
    assert fills[0].cloid == "0xabc"
    assert fills[0].price == Decimal("43000")  # filled at its limit price


def test_marketable_limit_fills_on_arrival_without_resting() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # A BUY LIMIT at 43000 is already above the market: it crosses on arrival
        # and fills immediately, rather than resting.
        await exchange.place(_limit_order("43000"))

    asyncio.run(scenario())

    assert statuses == []  # never rested, so no LIVE report
    assert len(fills) == 1
    assert fills[0].price == Decimal("43000")  # at the limit price
    assert fills[0].quantity == Decimal("1")


def test_ioc_limit_that_does_not_cross_is_cancelled_on_receipt() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # An IOC never rests: a BUY LIMIT at 41000 that can't fill now is
        # cancelled immediately, not left working on the book.
        await exchange.place(_limit_order("41000", time_in_force=TimeInForce.IOC))
        # A later crossing tick must not fill it — it is already gone.
        clock.advance_to(2_000)
        await bus.publish(_tick("41000", ts=2_000))

    asyncio.run(scenario())

    assert fills == []
    assert len(statuses) == 1
    assert statuses[0].status is OrderState.CANCELLED


def test_post_only_limit_that_would_cross_is_rejected_not_filled() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # A post_only BUY LIMIT at 43000 would cross on arrival (take liquidity);
        # post_only forbids that, so the venue rejects it outright.
        await exchange.place(_limit_order("43000", post_only=True))

    asyncio.run(scenario())

    assert fills == []
    assert len(statuses) == 1
    assert statuses[0].status is OrderState.REJECTED


def test_post_only_limit_that_does_not_cross_rests_normally() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # post_only only forbids crossing; a maker order that rests is fine.
        await exchange.place(_limit_order("41000", post_only=True))

    asyncio.run(scenario())

    assert fills == []
    assert [s.status for s in statuses] == [OrderState.LIVE]


def test_cancel_removes_a_resting_order_and_reports_cancelled() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        await exchange.place(_limit_order("41000"))  # rests
        statuses.clear()  # drop the LIVE report; we only care about the cancel
        await exchange.cancel("0xabc")
        # After cancel it is off the book: a later crossing tick must not fill it.
        clock.advance_to(2_000)
        await bus.publish(_tick("41000", ts=2_000))

    asyncio.run(scenario())

    assert fills == []
    assert [s.status for s in statuses] == [OrderState.CANCELLED]
    assert statuses[0].cloid == "0xabc"


def test_cancel_of_an_unknown_order_is_a_benign_no_op() -> None:
    exchange, bus, clock, fills, statuses = _limit_harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick("42000"))
        # Nothing resting under this cloid (already filled/cancelled or never
        # placed): the venue reports nothing and nothing raises.
        await exchange.cancel("0xdeadbeef")

    asyncio.run(scenario())

    assert fills == []
    assert statuses == []


def test_immediate_fill_model_is_full_fill_zero_slippage() -> None:
    model = ImmediateFillModel()
    fill = model.market_fill(_market_order(qty="3"), _tick("250"))
    assert fill.quantity == Decimal("3")
    assert fill.price == Decimal("250")


async def _record(sink: list, report: object) -> None:
    sink.append(report)
