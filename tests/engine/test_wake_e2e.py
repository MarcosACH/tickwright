"""Strategy wake-ups through the whole engine (issue #555, ADR-0062).

A strategy that declares a wake interval gets ``on_wake`` on every slot of
clock time, even when the feed sends no ticks. The test drives the
``ManualClock`` by hand, the way a replay feed would.
"""

import asyncio
from collections.abc import Iterable

from ledgers import GENESIS

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import MarketTick, OrderEvent, Wake
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


def _engine(clock: ManualClock, feed: _SilentFeed, bus: InMemoryBus | None = None) -> Engine:
    bus = bus or InMemoryBus()
    return Engine(
        bus=bus,
        clock=clock,
        store=SQLiteStore(":memory:"),
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
