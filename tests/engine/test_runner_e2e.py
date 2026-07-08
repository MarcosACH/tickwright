"""Engine runner E2E (issue #19): the supervised lifecycle over the real pipeline.

The ``Engine`` host (ADR-0014/0024) wired with every real v1 concrete —
``ReplayFeed`` → ``StrategyHost``-hosted strategy → ``ExecutionManager`` →
``PaperExchange`` over ``InMemoryBus`` + ``SQLiteStore`` — driven through
``run()``: ordered startup gated on the reconciliation barrier, supervised
operation, and a graceful stop that snapshots strategies and closes the store.
Zero external services; the whole run is on ``ManualClock``.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    ComponentState,
    MarketTick,
    OrderEvent,
    OrderFilled,
    OrderState,
    Side,
    derive_cloid,
)
from tickwright.engine.runner import Engine
from tickwright.strategies import SingleShotMarketStrategy

_ROWS = [
    {
        "symbol": "BTC",
        "price": "42000",
        "size": "3",
        "aggressor_side": "buy",
        "trade_id": "a",
        "ts_event": 1_000,
    },
    {
        "symbol": "BTC",
        "price": "42100",
        "size": "3",
        "aggressor_side": "sell",
        "trade_id": "b",
        "ts_event": 2_000,
    },
]


def _write_ticks(path: Path) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in _ROWS) + "\n")
    return path


def test_run_replays_trades_and_exits_zero_on_graceful_stop(tmp_path: Path) -> None:
    ticks = _write_ticks(tmp_path / "ticks.jsonl")
    db = tmp_path / "saga.db"

    async def main() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(db)
        exchange = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
        feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
        # Venue-sim wiring is composition-root business, not the Engine's: the
        # paper venue fills off the tick stream, a real venue would not.
        bus.subscribe(MarketTick, exchange.on_tick)
        engine = Engine(bus=bus, clock=clock, store=store, exchange=exchange, feed=feed)
        strategy = SingleShotMarketStrategy(
            strategy_id="trivial", bus=bus, clock=clock, side=Side.BUY, quantity=Decimal("0.5")
        )
        engine.register(strategy, symbols={"BTC"})
        assert engine.state is ComponentState.READY

        filled = asyncio.Event()

        async def on_order_event(event: OrderEvent) -> None:
            if isinstance(event, OrderFilled):
                filled.set()

        bus.subscribe(OrderEvent, on_order_event)

        run = asyncio.create_task(engine.run())
        # The engine keeps running after replay end-of-file — like the CLI, it
        # stops only when told to (SIGTERM there, ``stop()`` here).
        await asyncio.wait_for(filled.wait(), timeout=5)
        assert engine.state is ComponentState.RUNNING
        await engine.stop()
        return await run, engine

    exit_code, engine = asyncio.run(main())

    assert exit_code == 0
    assert engine.state is ComponentState.STOPPED

    # The graceful stop closed the store; the durable trail survives the process:
    # the saga is FILLED and the final strategy snapshot was taken (ADR-0016).
    reopened = SQLiteStore(db)
    try:
        order = reopened.get_order(derive_cloid("trivial:BTC:1"))
        assert order is not None
        assert order.state is OrderState.FILLED
        assert order.cum_qty == Decimal("0.5")
        assert reopened.load_strategy_snapshot("trivial") is not None
    finally:
        reopened.close()
