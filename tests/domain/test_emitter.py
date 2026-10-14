"""``SignalEmitter`` — the strategy-author's seq + publish helper (ADR-0016).

Concentrates the one piece of strategy mechanics that is a correctness spine and
must never be a strategy author's problem: the monotonic ``seq`` counter (so
``signal_id``s are deterministic and never reused), the clock-stamped envelope,
and the publish. These tests pin that mechanics once, so every strategy that
composes the emitter inherits it for free.
"""

import asyncio
from collections.abc import Callable, Coroutine
from decimal import Decimal
from typing import Any

import pytest

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    CancelSignal,
    InvariantViolation,
    OrderType,
    PlaceSignal,
    Side,
    Signal,
    SignalEmitter,
    TimeInForce,
)


def _harness() -> tuple[InMemoryBus, ManualClock, SignalEmitter, list[Signal]]:
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    store = SQLiteStore(":memory:")
    emitter = SignalEmitter(strategy_id="alpha", bus=bus, clock=clock, record_seq=store.raise_seq)
    signals: list[Signal] = []

    async def record(signal: Signal) -> None:
        signals.append(signal)

    bus.subscribe(Signal, record)
    return bus, clock, emitter, signals


async def _place(emitter: SignalEmitter, *, order_type: OrderType = OrderType.MARKET) -> str:
    return await emitter.place(
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=order_type,
        time_in_force=TimeInForce.IOC,
    )


def test_place_publishes_the_signal_and_returns_its_id() -> None:
    _, _, emitter, signals = _harness()

    signal_id = asyncio.run(_place(emitter))

    assert len(signals) == 1
    signal = signals[0]
    assert isinstance(signal, PlaceSignal)
    assert signal.strategy_id == "alpha"
    assert signal.symbol == "BTC"
    assert signal.side is Side.BUY
    assert signal.quantity == Decimal("0.5")
    assert signal.order_type is OrderType.MARKET
    # The emitter owns the seq: the first signal is seq 1, and it returns the id
    # the caller needs to later target the order for cancel.
    assert signal.signal_id == "alpha:BTC:1"
    assert signal_id == "alpha:BTC:1"


def test_place_carries_limit_price_and_post_only() -> None:
    _, _, emitter, signals = _harness()

    asyncio.run(
        emitter.place(
            symbol="BTC",
            side=Side.SELL,
            quantity=Decimal("1"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            price=Decimal("41000"),
            post_only=True,
        )
    )

    signal = signals[0]
    assert isinstance(signal, PlaceSignal)
    assert signal.price == Decimal("41000")
    assert signal.post_only is True


def test_seq_is_monotonic_across_places_and_cancels() -> None:
    bus, _, emitter, signals = _harness()

    async def scenario() -> None:
        await _place(emitter)  # seq 1
        await emitter.cancel(symbol="BTC", target_signal_id="alpha:BTC:1")  # seq 2
        await _place(emitter)  # seq 3

    asyncio.run(scenario())

    assert [s.signal_id for s in signals] == ["alpha:BTC:1", "alpha:BTC:2", "alpha:BTC:3"]
    assert isinstance(signals[1], CancelSignal)
    assert signals[1].target_signal_id == "alpha:BTC:1"


def test_set_next_seq_resumes_the_recovered_counter() -> None:
    _, _, emitter, signals = _harness()
    emitter.set_next_seq(7)

    signal_id = asyncio.run(_place(emitter))

    assert signal_id == "alpha:BTC:7"
    assert signals[0].signal_id == "alpha:BTC:7"


def test_every_signal_kind_is_in_the_seq_record_before_the_bus_sees_it() -> None:
    # On Kafka a crash can leave a signal in the topic with no saga behind it.
    # The restart reads the seq record, so it must already hold the signal's seq (#442).
    bus = InMemoryBus()
    store = SQLiteStore(":memory:")
    emitter = SignalEmitter(
        strategy_id="alpha", bus=bus, clock=ManualClock(start_ns=1_000), record_seq=store.raise_seq
    )
    record_at_delivery: list[tuple[int, int | None]] = []

    async def read_record(signal: Signal) -> None:
        record_at_delivery.append((signal.seq, store.seq_records().get("alpha")))

    bus.subscribe(Signal, read_record)

    async def scenario() -> None:
        await _place(emitter)
        await emitter.cancel(symbol="BTC", target_signal_id="alpha:BTC:1")
        await emitter.cancel_all(symbol="BTC")

    asyncio.run(scenario())

    assert record_at_delivery == [(1, 1), (2, 2), (3, 3)]


@pytest.mark.parametrize(
    "send",
    [
        _place,
        lambda emitter: emitter.cancel(symbol="BTC", target_signal_id="alpha:BTC:1"),
        lambda emitter: emitter.cancel_all(symbol="BTC"),
    ],
    ids=["place", "cancel", "cancel_all"],
)
def test_a_failed_seq_write_sends_no_signal(
    send: Callable[[SignalEmitter], Coroutine[Any, Any, str]],
) -> None:
    # A signal with no record is the #442 gap again. The write fails as
    # InvariantViolation, so the host lets it fault the engine (ADR-0024).
    bus = InMemoryBus()
    store = SQLiteStore(":memory:")
    emitter = SignalEmitter(
        strategy_id="alpha", bus=bus, clock=ManualClock(start_ns=1_000), record_seq=store.raise_seq
    )
    signals: list[Signal] = []

    async def record(signal: Signal) -> None:
        signals.append(signal)

    bus.subscribe(Signal, record)
    store.close()

    with pytest.raises(InvariantViolation, match="raise_seq"):
        asyncio.run(send(emitter))

    assert signals == []


def test_signals_are_stamped_from_the_clock() -> None:
    _, clock, emitter, signals = _harness()

    async def scenario() -> None:
        clock.advance_to(9_000)
        await _place(emitter)

    asyncio.run(scenario())

    assert signals[0].ts_event == 9_000
    assert signals[0].ts_init == 9_000
