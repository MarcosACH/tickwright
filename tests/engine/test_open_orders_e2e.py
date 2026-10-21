"""A strategy reads its own open orders (ADR-0041 §3), driven through the real engine.

The ``Engine`` with a scripted strategy over ``ReplayFeed``, ``PaperExchange``,
``InMemoryBus``, ``SQLiteStore`` and ``ManualClock``. The strategy acts on one
tick and reads ``Portfolio.open_orders()`` on a later one. No external service.
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
    OpenOrderView,
    OrderEvent,
    OrderState,
    OrderType,
    Portfolio,
    Side,
    SignalEmitter,
    TimeInForce,
)
from tickwright.engine.runner import Engine


class _ScriptedStrategy:
    """Runs one scripted step per tick and keeps every open-orders read."""

    def __init__(
        self, *, emitter: SignalEmitter, portfolio: Portfolio, steps: list["Step"]
    ) -> None:
        self.strategy_id = emitter.strategy_id
        self.emitter = emitter
        self.portfolio = portfolio
        self._steps = list(steps)
        self.reads: list[tuple[OpenOrderView, ...]] = []

    async def on_tick(self, tick: MarketTick) -> None:
        if self._steps:
            _, action = self._steps.pop(0)
            await action(self)

    async def on_order_event(self, event: OrderEvent) -> None:
        return None

    def set_next_seq(self, next_seq: int) -> None:
        self.emitter.set_next_seq(next_seq)

    def snapshot(self) -> bytes:
        return b""

    def restore(self, data: bytes) -> None:
        return None


# One step per tick: the tick's symbol, and what the strategy does when it arrives.
Step = tuple[str, Callable[[_ScriptedStrategy], Awaitable[None]]]


def _rest(symbol: str) -> Step:
    """A buy limit far below the replayed price, so it rests on the book."""

    async def step(strategy: _ScriptedStrategy) -> None:
        await strategy.emitter.place(
            symbol=symbol,
            side=Side.BUY,
            quantity=Decimal("0.1"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            price=Decimal("30000"),
        )

    return symbol, step


def _read(symbol: str) -> Step:
    async def step(strategy: _ScriptedStrategy) -> None:
        strategy.reads.append(strategy.portfolio.open_orders())

    return symbol, step


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


def _run(tmp_path: Path, steps: list[Step]) -> _ScriptedStrategy:
    """One engine life: replay one tick per step, then stop gracefully."""
    ticks = _write_ticks(tmp_path / "ticks.jsonl", [symbol for symbol, _ in steps])

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
            resting_orders=list,
        )
        feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
        engine = Engine(
            bus=bus,
            clock=clock,
            store=SQLiteStore(tmp_path / "saga.db"),
            exchange=exchange,
            feed=feed,
        )
        strategy = _ScriptedStrategy(
            emitter=engine.emitter_for("scripted"),
            portfolio=engine.portfolio_for("scripted"),
            steps=steps,
        )
        engine.register(strategy, symbols={"BTC", "ETH"})

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

    return asyncio.run(main())


def test_a_resting_limit_reads_back_with_its_placed_terms(tmp_path: Path) -> None:
    strategy = _run(tmp_path, [_rest("BTC"), _read("BTC")])

    assert strategy.reads == [
        (
            OpenOrderView(
                signal_id="scripted:BTC:1",
                symbol="BTC",
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                quantity=Decimal("0.1"),
                filled_quantity=Decimal("0"),
                price=Decimal("30000"),
                post_only=False,
                reduce_only=False,
                state=OrderState.LIVE,
                cancel_requested=False,
            ),
        )
    ]
