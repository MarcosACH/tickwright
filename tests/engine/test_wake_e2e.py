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


def _engine(clock: ManualClock, feed: _SilentFeed) -> Engine:
    bus = InMemoryBus()
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


async def _advance_and_wait(clock: ManualClock, strategy: _WakingStrategy, ts_ns: int) -> None:
    strategy.woke.clear()
    clock.advance_to(ts_ns)
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
            await _advance_and_wait(clock, strategy, ts * _S)
        await engine.stop()
        await run
        return [wake.ts_event for wake in strategy.wakes]

    assert asyncio.run(main()) == [15 * _S, 30 * _S, 45 * _S]
