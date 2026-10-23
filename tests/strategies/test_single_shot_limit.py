"""``SingleShotLimitStrategy`` — the resting-LIMIT + cancel reference strategy.

Proves the ``Strategy`` seam across the second order shape (issue #13): on its
first tick it emits one LIMIT ``PlaceSignal`` with a strategy-owned ``seq``; an
optional ``cancel_after_seconds`` makes it emit one ``CancelSignal`` targeting that
order on the first wake-up past its deadline (ADR-0062). It records the
``OrderFilled`` and ``OrderCancelled`` it receives. Cancel-by-``signal_id``: the
strategy never sees a ``cloid``.
"""

import asyncio
from decimal import Decimal

import pytest
from ledgers import ledger, scoped

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
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
    SignalEmitter,
    TimeInForce,
    Wake,
)
from tickwright.strategies import SingleShotLimitStrategy

_S = 1_000_000_000


def _wake(slot_s: int) -> Wake:
    return Wake(strategy_id="trivial", ts_event=slot_s * _S, ts_init=slot_s * _S)


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
    *, cancel_after_seconds: int | None = None
) -> tuple[InMemoryBus, ManualClock, SingleShotLimitStrategy, list[Signal]]:
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    strategy = SingleShotLimitStrategy(
        emitter=SignalEmitter(
            strategy_id="trivial",
            bus=bus,
            clock=clock,
            record_seq=SQLiteStore(":memory:").raise_seq,
        ),
        portfolio=scoped(ledger(SQLiteStore(":memory:")), "trivial"),
        settings=SingleShotLimitStrategy.Settings(
            side=Side.BUY,
            quantity=Decimal("0.5"),
            price=Decimal("41000"),
            cancel_after_seconds=cancel_after_seconds,
        ),
    )
    bus.subscribe(MarketTick, strategy.on_tick)
    bus.subscribe(Wake, strategy.on_wake)
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


@pytest.mark.parametrize(
    ("placed_at_ns", "first_cancel_wake_s"),
    [(2 * _S, 12), (2 * _S - 1, 12)],
    ids=["wake-at-the-deadline", "wake-past-the-deadline"],
)
def test_cancels_on_the_first_wake_up_at_or_past_the_deadline_and_only_once(
    placed_at_ns: int, first_cancel_wake_s: int
) -> None:
    bus, _, _, signals = _harness(cancel_after_seconds=10)

    async def scenario() -> list[list[type]]:
        await bus.publish(_tick(ts=placed_at_ns))
        seen = []
        for slot_s in (first_cancel_wake_s - 1, first_cancel_wake_s, first_cancel_wake_s + 1):
            await bus.publish(_wake(slot_s))
            seen.append([type(s) for s in signals])
        return seen

    before, at, after = asyncio.run(scenario())

    assert before == [PlaceSignal]
    assert at == [PlaceSignal, CancelSignal]
    assert after == [PlaceSignal, CancelSignal]
    cancel = signals[1]
    assert isinstance(cancel, CancelSignal)
    # Cancels by the signal_id it emitted; its own seq is fresh (ADR-0026).
    assert cancel.target_signal_id == "trivial:BTC:1"
    assert cancel.signal_id == "trivial:BTC:2"


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
    bus, _, strategy, signals = _harness(cancel_after_seconds=1)
    strategy.set_next_seq(4)

    asyncio.run(bus.publish(_tick(ts=1 * _S)))
    asyncio.run(bus.publish(_wake(2)))

    assert [s.signal_id for s in signals] == ["trivial:BTC:4", "trivial:BTC:5"]
    cancel = signals[1]
    assert isinstance(cancel, CancelSignal)
    assert cancel.target_signal_id == "trivial:BTC:4"


def test_restored_before_the_deadline_it_cancels_at_the_original_deadline() -> None:
    # Placed at 1 s, so the deadline is 11 s.
    bus, _, fired, _ = _harness(cancel_after_seconds=10)
    asyncio.run(bus.publish(_tick(ts=1 * _S)))

    second_bus, _, restored, signals = _harness(cancel_after_seconds=10)
    restored.restore(fired.snapshot())
    restored.set_next_seq(2)

    async def second_life() -> list[type]:
        # A tick at 5 s must not place again or restart the deadline at 15 s.
        await second_bus.publish(_tick(ts=5 * _S))
        for slot_s in range(5, 11):
            await second_bus.publish(_wake(slot_s))
        before = [type(s) for s in signals]
        await second_bus.publish(_wake(11))
        return before

    assert asyncio.run(second_life()) == []
    assert [type(s) for s in signals] == [CancelSignal]
    cancel = signals[0]
    assert isinstance(cancel, CancelSignal)
    assert cancel.target_signal_id == "trivial:BTC:1"
    assert cancel.signal_id == "trivial:BTC:2"
