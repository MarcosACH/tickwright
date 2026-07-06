"""``SingleShotLimitStrategy`` — the resting-LIMIT + cancel reference strategy.

Proves the ``Strategy`` seam across the second order shape (issue #13): on its
first tick it emits one LIMIT ``PlaceSignal`` with a strategy-owned ``seq``; an
optional ``cancel_after_ticks`` makes it emit one ``CancelSignal`` targeting that
order after a set number of later ticks. It records the ``OrderFilled`` and
``OrderCancelled`` it receives. Cancel-by-``signal_id``: the strategy never sees
a ``cloid``.
"""

import asyncio
from decimal import Decimal

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.domain import (
    AggressorSide,
    CancelSignal,
    MarketTick,
    OrderCancelled,
    OrderFilled,
    OrderType,
    PlaceSignal,
    Side,
    Signal,
    TimeInForce,
)
from tickwright.strategies import SingleShotLimitStrategy


def _tick(ts: int = 1_000, symbol: str = "BTC") -> MarketTick:
    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol=symbol,
        price=Decimal("42000"),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id=f"t{ts}",
        seq=0,
    )


def _harness(
    *, cancel_after_ticks: int | None = None
) -> tuple[InMemoryBus, ManualClock, SingleShotLimitStrategy, list[Signal]]:
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    strategy = SingleShotLimitStrategy(
        strategy_id="trivial",
        bus=bus,
        clock=clock,
        side=Side.BUY,
        quantity=Decimal("0.5"),
        price=Decimal("41000"),
        cancel_after_ticks=cancel_after_ticks,
    )
    bus.subscribe(MarketTick, strategy.on_tick)
    signals: list[Signal] = []
    bus.subscribe(Signal, lambda s: _record(signals, s))
    return bus, clock, strategy, signals


def test_emits_one_limit_signal_on_the_first_tick() -> None:
    bus, _, _, signals = _harness()

    asyncio.run(bus.publish(_tick()))

    assert len(signals) == 1
    signal = signals[0]
    assert isinstance(signal, PlaceSignal)
    assert signal.order_type is OrderType.LIMIT
    assert signal.side is Side.BUY
    assert signal.quantity == Decimal("0.5")
    assert signal.price == Decimal("41000")
    assert signal.time_in_force is TimeInForce.GTC
    assert signal.signal_id == "trivial:BTC:1"


def test_does_not_place_again_on_later_ticks() -> None:
    bus, clock, _, signals = _harness()

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick(ts=1_000))
        clock.advance_to(2_000)
        await bus.publish(_tick(ts=2_000))

    asyncio.run(scenario())
    assert [type(s) for s in signals] == [PlaceSignal]


def test_emits_a_cancel_targeting_its_own_order_after_the_configured_ticks() -> None:
    bus, clock, _, signals = _harness(cancel_after_ticks=1)

    async def scenario() -> None:
        clock.advance_to(1_000)
        await bus.publish(_tick(ts=1_000))  # places
        clock.advance_to(2_000)
        await bus.publish(_tick(ts=2_000))  # one tick later -> cancels

    asyncio.run(scenario())

    assert [type(s) for s in signals] == [PlaceSignal, CancelSignal]
    cancel = signals[1]
    assert isinstance(cancel, CancelSignal)
    # Cancels by the signal_id it emitted; its own seq is fresh (ADR-0026).
    assert cancel.target_signal_id == "trivial:BTC:1"
    assert cancel.signal_id == "trivial:BTC:2"


def test_cancels_only_once() -> None:
    bus, clock, _, signals = _harness(cancel_after_ticks=1)

    async def scenario() -> None:
        for ts in (1_000, 2_000, 3_000):
            clock.advance_to(ts)
            await bus.publish(_tick(ts=ts))

    asyncio.run(scenario())
    assert [type(s) for s in signals] == [PlaceSignal, CancelSignal]


def test_records_fills_and_cancellations() -> None:
    _, _, strategy, _ = _harness()
    filled = OrderFilled(
        ts_event=2,
        ts_init=2,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        trade_id="v1",
        quantity=Decimal("0.5"),
        price=Decimal("41000"),
        cum_qty=Decimal("0.5"),
    )
    cancelled = OrderCancelled(
        ts_event=3,
        ts_init=3,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
    )

    async def deliver() -> None:
        await strategy.on_order_event(filled)
        await strategy.on_order_event(cancelled)

    asyncio.run(deliver())

    assert strategy.fills == [filled]
    assert strategy.cancellations == [cancelled]


async def _record(sink: list, signal: object) -> None:
    sink.append(signal)


def test_set_next_seq_resumes_the_recovered_counter() -> None:
    bus, _, strategy, signals = _harness(cancel_after_ticks=1)
    strategy.set_next_seq(4)

    asyncio.run(bus.publish(_tick()))
    asyncio.run(bus.publish(_tick(ts=2_000)))

    assert [s.signal_id for s in signals] == ["trivial:BTC:4", "trivial:BTC:5"]
    cancel = signals[1]
    assert isinstance(cancel, CancelSignal)
    assert cancel.target_signal_id == "trivial:BTC:4"


def test_snapshot_round_trip_preserves_place_and_cancel_state() -> None:
    bus, _, fired, _ = _harness(cancel_after_ticks=2)
    asyncio.run(bus.publish(_tick()))

    second_bus, _, restored, signals = _harness(cancel_after_ticks=2)
    restored.restore(fired.snapshot())
    restored.set_next_seq(2)
    asyncio.run(second_bus.publish(_tick(ts=2_000)))
    asyncio.run(second_bus.publish(_tick(ts=3_000)))

    # No second placement; the cancel counts restart-spanning ticks and still
    # targets the signal_id placed in the first life.
    assert [type(s) for s in signals] == [CancelSignal]
    cancel = signals[0]
    assert isinstance(cancel, CancelSignal)
    assert cancel.target_signal_id == "trivial:BTC:1"
    assert cancel.signal_id == "trivial:BTC:2"
