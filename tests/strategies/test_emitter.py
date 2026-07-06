"""``SignalEmitter`` — the strategy-author's seq + publish helper (ADR-0016).

Concentrates the one piece of strategy mechanics that is a correctness spine and
must never be a strategy author's problem: the monotonic ``seq`` counter (so
``signal_id``s are deterministic and never reused), the clock-stamped envelope,
and the publish. These tests pin that mechanics once, so every strategy that
composes the emitter inherits it for free.
"""

import asyncio
from decimal import Decimal

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.domain import (
    CancelSignal,
    OrderType,
    PlaceSignal,
    Side,
    Signal,
    TimeInForce,
)
from tickwright.strategies import SignalEmitter


def _harness() -> tuple[InMemoryBus, ManualClock, SignalEmitter, list[Signal]]:
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    emitter = SignalEmitter(strategy_id="alpha", bus=bus, clock=clock)
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


def test_signals_are_stamped_from_the_clock() -> None:
    _, clock, emitter, signals = _harness()

    async def scenario() -> None:
        clock.advance_to(9_000)
        await _place(emitter)

    asyncio.run(scenario())

    assert signals[0].ts_event == 9_000
    assert signals[0].ts_init == 9_000
