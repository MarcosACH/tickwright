"""A strategy's cancel all (ADR-0055), driven through the real engine.

The ``Engine`` with a scripted strategy over ``ReplayFeed``, ``PaperExchange``,
``InMemoryBus``, ``SQLiteStore`` and ``ManualClock``. The strategy rests limit
orders, then sends one ``CancelAllSignal`` per symbol. No external service.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path

from ledgers import GENESIS

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    MarketTick,
    OrderCancelled,
    OrderEvent,
    OrderState,
    OrderType,
    Side,
    SignalEmitter,
    TimeInForce,
    derive_cloid,
)
from tickwright.engine.runner import Engine

# One step per tick: what the strategy does when that tick arrives.
Step = Callable[[SignalEmitter], Awaitable[object]]


class _ScriptedStrategy:
    """Runs one scripted step per tick and records every cancel it hears."""

    def __init__(self, *, bus: InMemoryBus, clock: ManualClock, steps: list[Step]) -> None:
        self.strategy_id = "scripted"
        self._emitter = SignalEmitter(strategy_id=self.strategy_id, bus=bus, clock=clock)
        self._steps = list(steps)
        self.cancellations: list[OrderCancelled] = []

    async def on_tick(self, tick: MarketTick) -> None:
        if self._steps:
            await self._steps.pop(0)(self._emitter)

    async def on_order_event(self, event: OrderEvent) -> None:
        if isinstance(event, OrderCancelled):
            self.cancellations.append(event)

    def set_next_seq(self, next_seq: int) -> None:
        self._emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return b""

    def restore(self, data: bytes) -> None:
        return None


def _rest(symbol: str) -> Step:
    """A buy limit far below the replayed price, so it rests on the book."""

    async def step(emitter: SignalEmitter) -> object:
        return await emitter.place(
            symbol=symbol,
            side=Side.BUY,
            quantity=Decimal("0.1"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            price=Decimal("30000"),
        )

    return step


def _cancel_all(symbol: str) -> Step:
    async def step(emitter: SignalEmitter) -> object:
        return await emitter.cancel_all(symbol=symbol)

    return step


def _write_ticks(path: Path, symbols: list[str]) -> Path:
    rows = [
        {
            "symbol": symbol,
            "price": "42000",
            "size": "3",
            "aggressor_side": "sell",
            "trade_id": f"t{i}",
            "ts_event": 1_000 * (i + 1),
        }
        for i, symbol in enumerate(symbols)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _run(tmp_path: Path, steps: list[Step]) -> tuple[_ScriptedStrategy, SQLiteStore]:
    """One engine life: replay one tick per step, then stop gracefully."""
    ticks = _write_ticks(tmp_path / "ticks.jsonl", ["BTC"] * len(steps))
    db = tmp_path / "saga.db"

    async def main() -> _ScriptedStrategy:
        bus = InMemoryBus()
        clock = ManualClock()
        exchange = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
            applied_fills=lambda cloid: (),
        )
        feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
        engine = Engine(bus=bus, clock=clock, store=SQLiteStore(db), exchange=exchange, feed=feed)
        strategy = _ScriptedStrategy(bus=bus, clock=clock, steps=steps)
        engine.register(strategy, symbols={"BTC"})

        done = asyncio.Event()

        async def on_tick(tick: MarketTick) -> None:
            if tick.trade_id == f"t{len(steps) - 1}":
                done.set()

        run = asyncio.create_task(engine.run())
        bus.subscribe(MarketTick, on_tick)
        await asyncio.wait_for(done.wait(), timeout=5)
        await engine.stop()
        assert await run == 0
        return strategy

    strategy = asyncio.run(main())
    return strategy, SQLiteStore(db)


def test_one_cancel_all_cancels_every_open_order_on_the_symbol(tmp_path: Path) -> None:
    steps = [_rest("BTC"), _rest("BTC"), _rest("BTC"), _cancel_all("BTC")]
    strategy, store = _run(tmp_path, steps)

    placed = [derive_cloid(f"scripted:BTC:{seq}") for seq in (1, 2, 3)]
    for cloid in placed:
        order = store.get_order(cloid)
        assert order is not None
        assert order.state is OrderState.CANCELLED
    assert sorted(event.cloid for event in strategy.cancellations) == sorted(placed)
