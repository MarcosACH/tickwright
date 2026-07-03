"""``ExecutionManager`` — the one engine-internal saga orchestrator (ADR-0015).

It subscribes to ``Signal``s and raw ``ExecutionReport``s: on a ``PlaceSignal`` it
derives the ``cloid`` from the ``signal_id``, records the ``PENDING`` intent, and
publishes ``OrderPlaced`` → ``OrderSubmitted`` while sending to the exchange; on
the resulting ``FillReport`` it applies the saga transition and publishes the
canonical ``OrderFilled``. The exchange is real (``PaperExchange``) — we never mock
our own classes.
"""

import asyncio
from decimal import Decimal

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.domain import (
    AggressorSide,
    ExecutionReport,
    FillReport,
    MarketTick,
    OrderEvent,
    OrderFilled,
    OrderPlaced,
    OrderSubmitted,
    PlaceSignal,
    Side,
    Signal,
    TimeInForce,
    derive_cloid,
)
from tickwright.domain.enums import OrderType
from tickwright.engine.execution import ExecutionManager


def _market_signal(seq: int = 1) -> PlaceSignal:
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=seq,
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def _tick(price: str = "42000") -> MarketTick:
    return MarketTick(
        ts_event=1_000,
        ts_init=1_000,
        symbol="BTC",
        price=Decimal(price),
        size=Decimal("10"),
        aggressor_side=AggressorSide.BUY,
        trade_id="t1",
        seq=0,
    )


def _harness() -> tuple[InMemoryBus, ManualClock, list[OrderEvent]]:
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
    manager = ExecutionManager(bus=bus, clock=clock, exchange=exchange)

    bus.subscribe(MarketTick, exchange.on_tick)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)

    order_events: list[OrderEvent] = []
    bus.subscribe(OrderEvent, lambda ev: _record(order_events, ev))
    return bus, clock, order_events


def test_place_signal_drives_placed_submitted_filled_in_order() -> None:
    bus, _, order_events = _harness()

    async def scenario() -> None:
        await bus.publish(_tick())
        await bus.publish(_market_signal())

    asyncio.run(scenario())

    assert [type(ev) for ev in order_events] == [OrderPlaced, OrderSubmitted, OrderFilled]


def test_cloid_is_derived_from_the_signal_id() -> None:
    bus, _, order_events = _harness()
    expected = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick())
        await bus.publish(_market_signal())

    asyncio.run(scenario())

    assert {ev.cloid for ev in order_events} == {expected}
    assert all(ev.signal_id == "trivial:BTC:1" for ev in order_events)


def test_order_filled_carries_the_fill_details() -> None:
    bus, _, order_events = _harness()

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_market_signal())

    asyncio.run(scenario())

    filled = next(ev for ev in order_events if isinstance(ev, OrderFilled))
    assert filled.price == Decimal("42000")
    assert filled.quantity == Decimal("0.5")
    assert filled.cum_qty == Decimal("0.5")
    assert filled.event_id == f"{filled.cloid}:fill:{filled.trade_id}"


def test_duplicate_fill_report_yields_a_single_order_filled() -> None:
    bus, _, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick())
        await bus.publish(_market_signal())
        # Redeliver the exact fill (same trade_id -> same event_id): the saga is
        # already FILLED, so this must not publish a second OrderFilled.
        await bus.publish(
            FillReport(
                ts_event=1_000,
                ts_init=1_000,
                cloid=cloid,
                symbol="BTC",
                trade_id=f"{cloid}-1",
                quantity=Decimal("0.5"),
                price=Decimal("42000"),
            )
        )

    asyncio.run(scenario())

    filled = [ev for ev in order_events if isinstance(ev, OrderFilled)]
    assert len(filled) == 1
    assert filled[0].cum_qty == Decimal("0.5")


def test_fill_report_for_an_unknown_cloid_is_dropped() -> None:
    bus, _, order_events = _harness()

    async def scenario() -> None:
        # A fill for an order this manager never placed (reconciliation's concern
        # once it lands). It must be dropped silently: no OrderFilled, no raise.
        await bus.publish(
            FillReport(
                ts_event=1_000,
                ts_init=1_000,
                cloid="0xdeadbeef",
                symbol="BTC",
                trade_id="stray-1",
                quantity=Decimal("0.5"),
                price=Decimal("42000"),
            )
        )

    asyncio.run(scenario())

    assert order_events == []


def test_duplicate_signal_does_not_place_a_second_order() -> None:
    bus, _, order_events = _harness()

    async def scenario() -> None:
        await bus.publish(_tick())
        await bus.publish(_market_signal(seq=1))
        await bus.publish(_market_signal(seq=1))  # same signal_id -> same cloid

    asyncio.run(scenario())

    # Only the first signal produces a saga; the resent one is a no-op.
    assert [type(ev) for ev in order_events] == [OrderPlaced, OrderSubmitted, OrderFilled]


async def _record(sink: list, event: object) -> None:
    sink.append(event)
