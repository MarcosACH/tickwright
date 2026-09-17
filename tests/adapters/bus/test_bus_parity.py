"""The cascade rules both ``EventBus`` backends must share (ADR-0023, ADR-0024).

Swapping the backend changes durability, never behavior. Every test here reads
the bus through its seam alone: ``subscribe``, ``publish``, ``drain``, ``start``
and ``close``. Nothing looks at a queue, a topic, or an offset. The rules:

- A publish from inside a handler is delivered after every subscriber of the
  current event ran. Breadth-first, not recursion.
- A top-level publish returns only once the cascade it began has quiesced.
- A handler fault surfaces in the publish that caused it, and the bus survives.
- A fault drops the undelivered tail, including what the faulting handler
  published. A later publish delivers only its own cascade.
- When two events in one cascade fault, the first fault is the one that surfaces.

What is Kafka-only (offset commits, the held record's timing on the topic, a
send that fails) stays in ``test_kafka_bus.py``. What is in-memory-only (the
hot path loading no wire stack) stays in ``test_inmemory_bus.py``.
"""

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal

import pytest
from bus_backends import BUS_BACKENDS, make_bus

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


def _tick(seq: int = 1, symbol: str = "BTC") -> MarketTick:
    return MarketTick(
        ts_event=seq,
        ts_init=seq,
        symbol=symbol,
        price=Decimal("100"),
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


async def _append(sink: list[str], label: str) -> None:
    sink.append(label)


def _run(bus: EventBus, scenario: Callable[[], Awaitable[None]]) -> None:
    """Run ``scenario`` inside the bus lifecycle, bounded so a wrong backend fails, not hangs."""

    async def lifecycle() -> None:
        await bus.start()
        try:
            await scenario()
        finally:
            await bus.close()

    asyncio.run(asyncio.wait_for(lifecycle(), timeout=5))


@pytest.mark.parametrize("backend", BUS_BACKENDS)
def test_a_reentrant_publish_is_delivered_breadth_first_not_by_recursion(backend: str) -> None:
    """A Signal emitted during tick dispatch is delivered after every MarketTick
    subscriber ran. Depth-first recursion would interleave them, and a MARKET
    order could fill against a stale cached tick."""
    bus = make_bus(backend)
    order: list[str] = []

    async def first_tick_handler(_: MarketTick) -> None:
        order.append("tick.first")
        await bus.publish(_signal())

    bus.subscribe(MarketTick, first_tick_handler)
    bus.subscribe(MarketTick, lambda ev: _append(order, "tick.second"))
    bus.subscribe(Signal, lambda ev: _append(order, "signal"))

    async def scenario() -> None:
        await bus.publish(_tick())
        await bus.drain()

    _run(bus, scenario)

    assert order == ["tick.first", "tick.second", "signal"]


@pytest.mark.parametrize("backend", BUS_BACKENDS)
def test_publish_returns_only_after_the_cascade_quiesces(backend: str) -> None:
    bus = make_bus(backend)
    order: list[str] = []

    async def tick_handler(_: MarketTick) -> None:
        await bus.publish(_signal())

    bus.subscribe(MarketTick, tick_handler)
    bus.subscribe(Signal, lambda ev: _append(order, "signal"))

    async def scenario() -> None:
        await bus.publish(_tick())
        order.append("after-publish")

    _run(bus, scenario)

    assert order == ["signal", "after-publish"]


@pytest.mark.parametrize("backend", BUS_BACKENDS)
def test_a_handler_fault_surfaces_in_the_causing_publish_and_the_bus_survives(
    backend: str,
) -> None:
    """Containment parity (ADR-0024): a raw handler exception propagates into
    the publish that caused it, once. The next publish delivers again."""
    bus = make_bus(backend)
    seen: list[MarketTick] = []

    async def brittle(event: MarketTick) -> None:
        if event.seq == 1:
            raise RuntimeError("handler broke an engine assumption")
        seen.append(event)

    bus.subscribe(MarketTick, brittle)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="handler broke"):
            await bus.publish(_tick(1))
        await bus.publish(_tick(2))
        await bus.drain()

    _run(bus, scenario)

    assert seen == [_tick(2)]


@pytest.mark.parametrize("backend", BUS_BACKENDS)
def test_a_handler_fault_drops_the_undelivered_cascade_tail(backend: str) -> None:
    """A fault drops what the faulting handler published, so a caught-and-
    continued publish cannot bleed the aborted cascade into a later one."""
    bus = make_bus(backend)
    delivered: list[str] = []
    exploded = False

    async def tick_handler(_: MarketTick) -> None:
        nonlocal exploded
        await bus.publish(_signal())  # the tail, published before the fault
        if not exploded:
            exploded = True
            raise RuntimeError("boom")

    bus.subscribe(MarketTick, tick_handler)
    bus.subscribe(Signal, lambda ev: _append(delivered, "signal"))

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="boom"):
            await bus.publish(_tick(1))
        await bus.drain()
        assert delivered == []  # the tail never reached its subscriber

        # A later clean publish delivers only its own cascade's signal. It would
        # be ["signal", "signal"] if the dropped tail were kept.
        await bus.publish(_tick(2))
        await bus.drain()

    _run(bus, scenario)

    assert delivered == ["signal"]


@pytest.mark.parametrize("backend", BUS_BACKENDS)
def test_when_two_events_in_one_cascade_fault_the_first_is_the_one_that_surfaces(
    backend: str,
) -> None:
    """Dispatch is in publish order and stops at the first fault, so the caller
    sees the first exception on both backends. Two same-symbol events share a
    Kafka partition, which is what keeps their order there (ADR-0028)."""
    bus = make_bus(backend)

    async def handler(event: MarketTick) -> None:
        if event.symbol == "seed":
            await bus.publish(_tick(1))
            await bus.publish(_tick(2))
        elif event.seq == 1:
            raise RuntimeError("first fault")
        elif event.seq == 2:
            raise RuntimeError("second fault")

    bus.subscribe(MarketTick, handler)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="first fault"):
            await bus.publish(_tick(seq=0, symbol="seed"))

    _run(bus, scenario)
