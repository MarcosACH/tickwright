"""``StrategyHost`` (issue #17): the engine-side strategy runtime — registry,
per-strategy routing, the monotonic tick gate, containment, and snapshot/seq
recovery (ADR-0016/0018/0024/0025).

The recording strategy here is a third-party ``Strategy`` implementation, the
seam this host exists to serve — not a mock of an engine class.
"""

import asyncio
from decimal import Decimal

import pytest

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.domain import (
    AggressorSide,
    InvariantViolation,
    MarketTick,
    OrderEvent,
    OrderPlaced,
)
from tickwright.engine.strategy_host import StrategyHost


class RecordingStrategy:
    """A minimal third-party strategy that records what the host delivers."""

    def __init__(self, strategy_id: str) -> None:
        self.strategy_id = strategy_id
        self.ticks: list[MarketTick] = []
        self.order_events: list[OrderEvent] = []

    async def on_tick(self, tick: MarketTick) -> None:
        self.ticks.append(tick)

    async def on_order_event(self, event: OrderEvent) -> None:
        self.order_events.append(event)


def _tick(symbol: str, *, ts: int = 1_000, trade_id: str = "a", seq: int = 1) -> MarketTick:
    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol=symbol,
        price=Decimal("100"),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id=trade_id,
        seq=seq,
    )


def test_strategy_receives_only_ticks_for_its_declared_symbols() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock())
    strategy = RecordingStrategy("btc-strat")
    host.register(strategy, symbols={"BTC"})
    host.start()

    asyncio.run(bus.publish(_tick("BTC")))
    asyncio.run(bus.publish(_tick("ETH")))

    assert [tick.symbol for tick in strategy.ticks] == ["BTC"]


def _order_placed(strategy_id: str, *, symbol: str = "BTC") -> OrderPlaced:
    signal_id = f"{strategy_id}:{symbol}:1"
    return OrderPlaced(
        ts_event=1_000,
        ts_init=1_000,
        cloid=f"0x{strategy_id}",
        strategy_id=strategy_id,
        signal_id=signal_id,
        symbol=symbol,
    )


def test_strategy_receives_only_its_own_order_events() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock())
    alpha = RecordingStrategy("alpha")
    beta = RecordingStrategy("beta")
    host.register(alpha, symbols={"BTC"})
    host.register(beta, symbols={"BTC"})
    host.start()

    asyncio.run(bus.publish(_order_placed("alpha")))
    asyncio.run(bus.publish(_order_placed("beta")))

    assert [event.strategy_id for event in alpha.order_events] == ["alpha"]
    assert [event.strategy_id for event in beta.order_events] == ["beta"]


def test_duplicate_and_out_of_order_ticks_never_reach_on_tick() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock())
    strategy = RecordingStrategy("gated")
    host.register(strategy, symbols={"BTC"})
    host.start()

    asyncio.run(bus.publish(_tick("BTC", ts=1_000, trade_id="a")))
    asyncio.run(bus.publish(_tick("BTC", ts=1_000, trade_id="a")))  # duplicate
    asyncio.run(bus.publish(_tick("BTC", ts=900, trade_id="z")))  # out of order
    asyncio.run(bus.publish(_tick("BTC", ts=2_000, trade_id="b")))

    assert [(t.ts_event, t.trade_id) for t in strategy.ticks] == [(1_000, "a"), (2_000, "b")]


def test_tick_gate_is_independent_per_symbol() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock())
    strategy = RecordingStrategy("multi")
    host.register(strategy, symbols={"BTC", "ETH"})
    host.start()

    asyncio.run(bus.publish(_tick("BTC", ts=2_000, trade_id="a")))
    # ETH is earlier than BTC's high-water; its own gate has seen nothing yet.
    asyncio.run(bus.publish(_tick("ETH", ts=500, trade_id="b")))

    assert [tick.symbol for tick in strategy.ticks] == ["BTC", "ETH"]


def test_duplicate_strategy_id_registration_fails_fast() -> None:
    host = StrategyHost(bus=InMemoryBus(), clock=ManualClock())
    host.register(RecordingStrategy("dup"), symbols={"BTC"})

    with pytest.raises(InvariantViolation, match="dup"):
        host.register(RecordingStrategy("dup"), symbols={"ETH"})
