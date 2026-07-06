"""``StrategyHost`` (issue #17): the engine-side strategy runtime — registry,
per-strategy routing, the monotonic tick gate, containment, and snapshot/seq
recovery (ADR-0016/0018/0024/0025).

The recording strategy here is a third-party ``Strategy`` implementation, the
seam this host exists to serve — not a mock of an engine class.
"""

import asyncio
from decimal import Decimal

import pytest
import structlog.testing

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


def test_stale_beyond_threshold_tick_is_dropped() -> None:
    bus = InMemoryBus()
    clock = ManualClock(start_ns=10_000)
    host = StrategyHost(bus=bus, clock=clock, tick_staleness_ns=1_000)
    strategy = RecordingStrategy("live")
    host.register(strategy, symbols={"BTC"})
    host.start()

    # 10_000 - 8_000 > 1_000: a pre-crash backlog tick — never trade on it.
    asyncio.run(bus.publish(_tick("BTC", ts=8_000, trade_id="a")))
    # 10_000 - 9_500 <= 1_000: fresh enough.
    asyncio.run(bus.publish(_tick("BTC", ts=9_500, trade_id="b")))

    assert [tick.trade_id for tick in strategy.ticks] == ["b"]


def test_staleness_gate_is_off_by_default() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(start_ns=10_000))
    strategy = RecordingStrategy("replay")
    host.register(strategy, symbols={"BTC"})
    host.start()

    asyncio.run(bus.publish(_tick("BTC", ts=1_000, trade_id="a")))

    assert [tick.trade_id for tick in strategy.ticks] == ["a"]


class RaisingStrategy(RecordingStrategy):
    """A third-party strategy whose handlers raise — the containment target."""

    def __init__(self, strategy_id: str, error: Exception) -> None:
        super().__init__(strategy_id)
        self._error = error

    async def on_tick(self, tick: MarketTick) -> None:
        raise self._error

    async def on_order_event(self, event: OrderEvent) -> None:
        raise self._error


def test_raising_strategy_emits_strategy_error_and_others_keep_receiving() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock())
    bad = RaisingStrategy("bad", KeyError("third-party bug"))
    good = RecordingStrategy("good")
    host.register(bad, symbols={"BTC"})
    host.register(good, symbols={"BTC"})
    host.start()

    tick = _tick("BTC", ts=1_000, trade_id="a")
    with structlog.testing.capture_logs() as logs:
        asyncio.run(bus.publish(tick))

    assert [t.trade_id for t in good.ticks] == ["a"]
    errors = [log for log in logs if log["event"] == "strategy.error"]
    assert len(errors) == 1
    assert errors[0]["strategy_id"] == "bad"
    assert errors[0]["event_id"] == tick.event_id


def test_raising_on_order_event_is_contained_too() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock())
    bad = RaisingStrategy("bad", ValueError("boom"))
    host.register(bad, symbols={"BTC"})
    host.start()

    event = _order_placed("bad")
    with structlog.testing.capture_logs() as logs:
        asyncio.run(bus.publish(event))

    errors = [log for log in logs if log["event"] == "strategy.error"]
    assert len(errors) == 1
    assert errors[0]["event_id"] == event.event_id


def test_invariant_violation_pierces_containment() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock())
    host.register(
        RaisingStrategy("bad", InvariantViolation("broken engine assumption")), symbols={"BTC"}
    )
    host.start()

    with pytest.raises(InvariantViolation):
        asyncio.run(bus.publish(_tick("BTC")))


def test_duplicate_strategy_id_registration_fails_fast() -> None:
    host = StrategyHost(bus=InMemoryBus(), clock=ManualClock())
    host.register(RecordingStrategy("dup"), symbols={"BTC"})

    with pytest.raises(InvariantViolation, match="dup"):
        host.register(RecordingStrategy("dup"), symbols={"ETH"})
