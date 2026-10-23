"""Strategy wake-ups through the whole engine (issue #555, ADR-0062).

A strategy that declares a wake interval gets ``on_wake`` on every slot of
clock time, even when the feed sends no ticks. Most tests drive the
``ManualClock`` by hand, the way a replay feed would. The ordering test runs a
real replay.
"""

import asyncio
import json
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path

import pytest
from bus_backends import BUS_BACKENDS, make_bus
from ledgers import GENESIS

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AggressorSide,
    EventBus,
    MarketFeed,
    MarketTick,
    OrderDenied,
    OrderEvent,
    OrderType,
    Side,
    SignalEmitter,
    TimeInForce,
    Wake,
)
from tickwright.engine.guard import RealGuard
from tickwright.engine.runner import Engine

_S = 1_000_000_000


class _WakingStrategy:
    """Records every wake-up it gets."""

    def __init__(self, strategy_id: str, *, wake_interval_seconds: float | None) -> None:
        self.strategy_id = strategy_id
        self.wake_interval_seconds = wake_interval_seconds
        self.wakes: list[Wake] = []
        self.woke = asyncio.Event()

    async def on_tick(self, tick: MarketTick) -> None:
        return None

    async def on_wake(self, wake: Wake) -> None:
        self.wakes.append(wake)
        self.woke.set()

    async def on_order_event(self, event: OrderEvent) -> None:
        return None

    def set_next_seq(self, next_seq: int) -> None:
        return None

    def snapshot(self) -> bytes:
        return b""

    def restore(self, data: bytes) -> None:
        return None


class _SilentFeed:
    """A live feed that never sends a tick. ``started`` fires once its loop
    runs, which is after every wake loop is parked on its first slot."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    def add_symbols(self, symbols: Iterable[str]) -> None:
        return None

    async def start(self) -> None:
        return None

    async def run(self) -> None:
        self.started.set()
        await asyncio.Event().wait()

    async def stop(self) -> None:
        return None


def _engine(
    clock: ManualClock,
    feed: MarketFeed,
    bus: EventBus | None = None,
    *,
    store: SQLiteStore | None = None,
    guard: RealGuard | None = None,
) -> Engine:
    bus = bus or InMemoryBus()
    return Engine(
        bus=bus,
        clock=clock,
        store=store or SQLiteStore(":memory:"),
        guard=guard,
        exchange=PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            instrument_specs={},
            account_net=dict,
            applied_fills=lambda cloid: (),
            resting_orders=list,
        ),
        feed=feed,
    )


async def _advance_and_wait(clock: ManualClock, ts_ns: int, *waking: _WakingStrategy) -> None:
    """Move the clock, then wait until each of ``waking`` has had its wake-up."""
    for strategy in waking:
        strategy.woke.clear()
    clock.advance_to(ts_ns)
    for strategy in waking:
        await asyncio.wait_for(strategy.woke.wait(), timeout=5)


def test_a_strategy_wakes_on_each_slot_of_its_interval_with_no_ticks() -> None:
    async def main() -> list[int]:
        clock = ManualClock(start_ns=7 * _S)
        feed = _SilentFeed()
        engine = _engine(clock, feed)
        strategy = _WakingStrategy("waker", wake_interval_seconds=15.0)
        engine.register(strategy, symbols={"BTC"})
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(feed.started.wait(), timeout=5)
        for ts in (15, 30, 45):
            await _advance_and_wait(clock, ts * _S, strategy)
        await engine.stop()
        await run
        return [wake.ts_event for wake in strategy.wakes]

    assert asyncio.run(main()) == [15 * _S, 30 * _S, 45 * _S]


def test_each_strategy_gets_only_its_own_wake_ups_and_a_none_strategy_gets_none() -> None:
    async def main() -> tuple[list[int], list[int], list[int], set[str]]:
        clock = ManualClock(start_ns=7 * _S)
        feed = _SilentFeed()
        bus = InMemoryBus()
        published: set[str] = set()

        async def record(wake: Wake) -> None:
            published.add(wake.strategy_id)

        bus.subscribe(Wake, record)
        engine = _engine(clock, feed, bus)
        fast = _WakingStrategy("fast", wake_interval_seconds=10.0)
        slow = _WakingStrategy("slow", wake_interval_seconds=15.0)
        never = _WakingStrategy("never", wake_interval_seconds=None)
        engine.register(fast, symbols={"BTC"})
        engine.register(slow, symbols={"ETH"})
        engine.register(never, symbols={"SOL"})
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(feed.started.wait(), timeout=5)
        await _advance_and_wait(clock, 10 * _S, fast)
        await _advance_and_wait(clock, 15 * _S, slow)
        await _advance_and_wait(clock, 20 * _S, fast)
        await _advance_and_wait(clock, 30 * _S, fast, slow)
        await engine.stop()
        await run
        return (
            [wake.ts_event for wake in fast.wakes],
            [wake.ts_event for wake in slow.wakes],
            [wake.ts_event for wake in never.wakes],
            published,
        )

    fast, slow, never, published = asyncio.run(main())
    assert fast == [10 * _S, 20 * _S, 30 * _S]
    assert slow == [15 * _S, 30 * _S]
    assert never == []
    assert published == {"fast", "slow"}


class _PlacingOnWakeStrategy(_WakingStrategy):
    """Places one resting buy on its first wake-up."""

    def __init__(self, strategy_id: str, emitter: SignalEmitter) -> None:
        super().__init__(strategy_id, wake_interval_seconds=15.0)
        self._emitter = emitter

    async def on_wake(self, wake: Wake) -> None:
        await self._emitter.place(
            symbol="BTC",
            side=Side.BUY,
            quantity=Decimal("0.01"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            price=Decimal("41000"),
        )
        await super().on_wake(wake)


def test_an_order_placed_from_on_wake_with_the_kill_switch_on_is_denied() -> None:
    """A wake-up comes from the clock, not the feed, but its order takes the same
    path through the guard (ADR-0026)."""

    async def main() -> list[OrderEvent]:
        clock = ManualClock(start_ns=7 * _S)
        feed = _SilentFeed()
        bus = InMemoryBus()
        store = SQLiteStore(":memory:")
        guard = RealGuard(specs={}, store=store, clock=clock)
        events: list[OrderEvent] = []
        denied = asyncio.Event()

        async def record(event: OrderEvent) -> None:
            events.append(event)
            if isinstance(event, OrderDenied):
                denied.set()

        bus.subscribe(OrderEvent, record)
        engine = _engine(clock, feed, bus, store=store, guard=guard)
        strategy = _PlacingOnWakeStrategy("waker", engine.emitter_for("waker"))
        engine.register(strategy, symbols={"BTC"})
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(feed.started.wait(), timeout=5)
        guard.trip_kill_switch("operator halt")
        clock.advance_to(15 * _S)
        await asyncio.wait_for(denied.wait(), timeout=5)
        await engine.stop()
        await run
        return events

    events = asyncio.run(main())
    assert [type(event) for event in events] == [OrderDenied]
    denied = events[0]
    assert isinstance(denied, OrderDenied)
    assert denied.reason == "kill switch tripped"


class _SeesBoth(_WakingStrategy):
    """Records ticks and wake-ups in the order they arrive."""

    def __init__(self, *, last_tick_ns: int) -> None:
        super().__init__("both", wake_interval_seconds=15.0)
        self.seen: list[tuple[str, int]] = []
        self.done = asyncio.Event()
        self._last_tick_ns = last_tick_ns

    async def on_tick(self, tick: MarketTick) -> None:
        self.seen.append(("tick", tick.ts_event))
        if tick.ts_event == self._last_tick_ns:
            self.done.set()

    async def on_wake(self, wake: Wake) -> None:
        self.seen.append(("wake", wake.ts_event))


def test_a_wake_up_reaches_the_strategy_before_a_tick_at_the_same_time(tmp_path: Path) -> None:
    """A replay is the same story every time it runs (ADR-0027). So when a tick
    lands on a slot, the order of the two cannot be left to chance. The wake-up
    goes first, like a cadence that comes due at a tick's own time (ADR-0033)."""
    ticks = tmp_path / "ticks.jsonl"
    rows = [
        {
            "symbol": "BTC",
            "price": "42000",
            "size": "1",
            "aggressor_side": "buy",
            "trade_id": f"t{seconds}",
            "ts_event": seconds * _S,
        }
        for seconds in (7, 15, 22, 30)
    ]
    ticks.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    async def main() -> list[tuple[str, int]]:
        clock = ManualClock()
        bus = InMemoryBus()
        engine = _engine(clock, ReplayFeed(path=ticks, bus=bus, clock=clock), bus)
        strategy = _SeesBoth(last_tick_ns=30 * _S)
        engine.register(strategy, symbols={"BTC"})
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(strategy.done.wait(), timeout=5)
        await engine.stop()
        await run
        return strategy.seen

    expected = [
        ("tick", 7 * _S),
        ("wake", 15 * _S),
        ("tick", 15 * _S),
        ("tick", 22 * _S),
        ("wake", 30 * _S),
        ("tick", 30 * _S),
    ]
    assert asyncio.run(main()) == expected
    assert asyncio.run(main()) == expected


class _OneTickFeed:
    """A live feed that sends one tick, then goes quiet."""

    def __init__(self, bus: EventBus, tick: MarketTick) -> None:
        self._bus = bus
        self._tick = tick

    def add_symbols(self, symbols: Iterable[str]) -> None:
        return None

    async def start(self) -> None:
        return None

    async def run(self) -> None:
        await self._bus.publish(self._tick)
        await asyncio.Event().wait()

    async def stop(self) -> None:
        return None


class _PausesInOnTick(_WakingStrategy):
    """Parks inside ``on_tick`` until the test lets it go."""

    def __init__(self) -> None:
        super().__init__("pauser", wake_interval_seconds=15.0)
        self.seen: list[str] = []
        self.paused = asyncio.Event()
        self.release = asyncio.Event()

    async def on_tick(self, tick: MarketTick) -> None:
        self.seen.append("tick starts")
        self.paused.set()
        await self.release.wait()
        self.seen.append("tick ends")

    async def on_wake(self, wake: Wake) -> None:
        self.seen.append("wake")
        await super().on_wake(wake)


@pytest.mark.parametrize("backend", BUS_BACKENDS)
def test_on_wake_never_runs_while_on_tick_is_paused(backend: str) -> None:
    """A strategy author writes ``on_tick`` and ``on_wake`` without a lock. That
    is safe only because each bus hands over one event at a time (ADR-0062)."""

    async def main() -> list[str]:
        clock = ManualClock(start_ns=7 * _S)
        bus = make_bus(backend)
        tick = MarketTick(
            ts_event=7 * _S,
            ts_init=7 * _S,
            symbol="BTC",
            price=Decimal("42000"),
            size=Decimal("1"),
            aggressor_side=AggressorSide.BUY,
            trade_id="t1",
            seq=0,
        )
        engine = _engine(clock, _OneTickFeed(bus, tick), bus)
        strategy = _PausesInOnTick()
        engine.register(strategy, symbols={"BTC"})
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(strategy.paused.wait(), timeout=5)
        # The slot comes due while on_tick is parked. The wake loop publishes now.
        clock.advance_to(15 * _S)
        for _ in range(20):
            await asyncio.sleep(0)
        strategy.release.set()
        await asyncio.wait_for(strategy.woke.wait(), timeout=5)
        await engine.stop()
        await run
        return strategy.seen

    assert asyncio.run(main()) == ["tick starts", "tick ends", "wake"]
