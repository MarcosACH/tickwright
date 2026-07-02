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


def _harness() -> tuple[PaperExchange, InMemoryBus, ManualClock, list[FillReport]]:
    bus = InMemoryBus()
    clock = ManualClock()
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
    bus.subscribe(MarketTick, exchange.on_tick)
    reports: list[FillReport] = []
    bus.subscribe(FillReport, lambda r: _record(reports, r))
    return exchange, bus, clock, reports


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


def test_limit_orders_are_not_supported_in_the_tracer_slice() -> None:
    exchange, bus, clock, _ = _harness()
    limit = PlaceOrder(
        cloid="0xabc",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("41000"),
    )
    with pytest.raises(NotImplementedError):
        asyncio.run(exchange.place(limit))


def test_immediate_fill_model_is_full_fill_zero_slippage() -> None:
    model = ImmediateFillModel()
    fill = model.market_fill(_market_order(qty="3"), _tick("250"))
    assert fill.quantity == Decimal("3")
    assert fill.price == Decimal("250")


async def _record(sink: list, report: object) -> None:
    sink.append(report)
