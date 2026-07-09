"""InMemoryBus dispatch semantics (ADR-0023).

Dispatch is synchronous and inline: ``publish`` awaits each matching subscriber
in subscription order. A ``publish`` called from *inside* a handler appends to one
central FIFO and returns; the top-level call drains the whole cascade to
quiescence. That FIFO trampoline — not depth-first recursion — is what keeps the
in-memory cascade order-independent and parity-locked with the Kafka poll loop.
"""

import asyncio
import subprocess
import sys
from decimal import Decimal

import pytest

from tickwright.adapters.bus import InMemoryBus
from tickwright.domain import (
    EventBus,
    MarketTick,
    OrderType,
    PlaceSignal,
    Side,
    Signal,
    TimeInForce,
)
from tickwright.domain.enums import AggressorSide


def _tick(seq: int = 1, price: str = "100") -> MarketTick:
    return MarketTick(
        ts_event=seq,
        ts_init=seq,
        symbol="BTC",
        price=Decimal(price),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id=f"t{seq}",
        seq=seq,
    )


def _signal(seq: int = 1) -> PlaceSignal:
    return PlaceSignal(
        ts_event=seq,
        ts_init=seq,
        strategy_id="s",
        symbol="BTC",
        seq=seq,
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def test_the_in_memory_hot_path_imports_no_serde_or_kafka_module() -> None:
    """ADR-0025: events pass by reference with zero serialization, so the
    hermetic default must never load the wire stack. Proven on a fresh
    interpreter: importing the bus package and InMemoryBus pulls in neither
    msgspec/aiokafka nor the serde/kafka modules. (The import-linter contract
    gates the static graph; this gates what actually loads at runtime.)"""
    probe = (
        "import sys\n"
        "from tickwright.adapters.bus import InMemoryBus\n"
        "loaded = [m for m in sys.modules if m.startswith(('msgspec', 'aiokafka'))\n"
        "          or m.endswith(('.serde', '.kafka'))]\n"
        "assert not loaded, f'wire stack leaked onto the hot path: {loaded}'\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)


def test_lifecycle_is_a_no_op_and_satisfies_the_eventbus_seam() -> None:
    # The seam carries lifecycle (ADR-0024 starts and stops the bus); for the
    # in-memory backend both verbs are awaitable no-ops — nothing to connect,
    # nothing to flush — and publish works regardless of lifecycle state.
    bus = InMemoryBus()
    assert isinstance(bus, EventBus)
    seen: list[MarketTick] = []
    bus.subscribe(MarketTick, lambda ev: _record(seen, ev))

    async def scenario() -> None:
        await bus.start()
        await bus.publish(_tick())
        await bus.drain()  # nothing can be in flight: publish already drained
        await bus.close()

    asyncio.run(scenario())

    assert seen == [_tick()]


def test_publish_delivers_to_subscribers_of_matching_type() -> None:
    bus = InMemoryBus()
    seen: list[MarketTick] = []
    bus.subscribe(MarketTick, lambda ev: _record(seen, ev))

    tick = _tick()
    asyncio.run(bus.publish(tick))

    assert seen == [tick]


def test_type_mismatch_is_not_delivered() -> None:
    bus = InMemoryBus()
    seen: list[object] = []
    bus.subscribe(MarketTick, lambda ev: _record(seen, ev))

    asyncio.run(bus.publish(_signal()))

    assert seen == []


def test_subscribing_to_a_base_family_catches_subclasses() -> None:
    # ExecutionManager subscribes to the Signal base to catch PlaceSignal.
    bus = InMemoryBus()
    seen: list[Signal] = []
    bus.subscribe(Signal, lambda ev: _record(seen, ev))

    signal = _signal()
    asyncio.run(bus.publish(signal))

    assert seen == [signal]


def test_handlers_run_in_subscription_order() -> None:
    bus = InMemoryBus()
    order: list[str] = []
    bus.subscribe(MarketTick, lambda ev: _append(order, "first"))
    bus.subscribe(MarketTick, lambda ev: _append(order, "second"))

    asyncio.run(bus.publish(_tick()))

    assert order == ["first", "second"]


def test_reentrant_publish_is_breadth_first_not_recursion() -> None:
    """The cascade-order acceptance: a signal emitted during tick dispatch is
    delivered via the FIFO, so every MarketTick subscriber runs before the
    signal it spawned. Depth-first recursion would interleave them."""
    bus = InMemoryBus()
    order: list[str] = []

    async def first_tick_handler(_: MarketTick) -> None:
        order.append("tick.first")
        # Reentrant publish mid-dispatch — must enqueue, not recurse.
        await bus.publish(_signal())

    bus.subscribe(MarketTick, first_tick_handler)
    bus.subscribe(MarketTick, lambda ev: _append(order, "tick.second"))
    bus.subscribe(Signal, lambda ev: _append(order, "signal"))

    asyncio.run(bus.publish(_tick()))

    # Breadth-first: both tick subscribers, THEN the reentrantly-published signal.
    assert order == ["tick.first", "tick.second", "signal"]


def test_publish_returns_only_after_the_cascade_quiesces() -> None:
    bus = InMemoryBus()
    order: list[str] = []

    async def tick_handler(_: MarketTick) -> None:
        await bus.publish(_signal())

    bus.subscribe(MarketTick, tick_handler)
    bus.subscribe(Signal, lambda ev: _append(order, "signal"))

    async def scenario() -> None:
        await bus.publish(_tick())
        # The whole cascade has drained by the time the top-level publish returns.
        order.append("after-publish")

    asyncio.run(scenario())

    assert order == ["signal", "after-publish"]


def test_publish_with_no_subscribers_is_a_noop() -> None:
    bus = InMemoryBus()
    asyncio.run(bus.publish(_tick()))  # does not raise


def test_handler_exception_mid_drain_drops_the_undelivered_tail() -> None:
    """A handler failing mid-drain (fail-fast, ADR-0014) clears the FIFO, so a
    caught-and-continued publish can't bleed the aborted cascade's stale events
    into a later, unrelated one."""
    bus = InMemoryBus()
    delivered: list[str] = []
    exploded = False

    async def tick_handler(_: MarketTick) -> None:
        nonlocal exploded
        await bus.publish(_signal())  # enqueue a reentrant signal, then fail
        if not exploded:
            exploded = True
            raise RuntimeError("boom")

    bus.subscribe(MarketTick, tick_handler)
    bus.subscribe(Signal, lambda ev: _append(delivered, "signal"))

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(bus.publish(_tick()))
    # The first cascade aborted before the drain reached its reentrant signal.
    assert delivered == []

    # A later clean publish delivers only its own cascade's signal — the dropped
    # tail is gone, not resurrected (would be ["signal", "signal"] if retained).
    asyncio.run(bus.publish(_tick(seq=2)))
    assert delivered == ["signal"]


# ---- helpers ---------------------------------------------------------------


async def _record(sink: list, event: object) -> None:
    sink.append(event)


async def _append(sink: list[str], label: str) -> None:
    sink.append(label)
