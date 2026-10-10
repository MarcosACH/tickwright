"""Operator cancel all (ADR-0052, ADR-0060): the ``Engine`` as a one-shot exit run.

The real paper path, with no strategy: ``PaperExchange`` over ``InMemoryBus`` and
``SQLiteStore``, on ``ManualClock``. The run boots and reconciles like a normal
run, then does its one job and exits with the ADR-0060 code.
"""

import asyncio
import json
from pathlib import Path

from ledgers import GENESIS

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import MarketTick, OrderEvent
from tickwright.engine.exit_run import OperatorCancelAll
from tickwright.engine.runner import Engine
from tickwright.observability.testing import capture_events


class _StrategyThatMustNotStart:
    """Records whether the host ever started or snapshotted it."""

    def __init__(self) -> None:
        self.strategy_id = "resting"
        self.started = False
        self.snapshotted = False

    async def on_tick(self, tick: MarketTick) -> None:
        return None

    async def on_order_event(self, event: OrderEvent) -> None:
        return None

    def set_next_seq(self, next_seq: int) -> None:
        self.started = True

    def snapshot(self) -> bytes:
        self.snapshotted = True
        return b""

    def restore(self, data: bytes) -> None:
        self.started = True


def _ticks(path: Path) -> Path:
    row = {
        "symbol": "BTC",
        "price": "42000",
        "size": "1",
        "aggressor_side": "buy",
        "trade_id": "a",
        "ts_event": 1_000,
    }
    path.write_text(json.dumps(row) + "\n")
    return path


def test_an_exit_run_on_an_empty_account_starts_nothing_and_exits_done(tmp_path: Path) -> None:
    bus = InMemoryBus()
    clock = ManualClock()
    store = SQLiteStore(tmp_path / "saga.db")
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
        applied_fills=lambda cloid: (),
    )
    feed = ReplayFeed(path=_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock)
    engine = Engine(
        bus=bus,
        clock=clock,
        store=store,
        exchange=exchange,
        feed=feed,
        exit_job=OperatorCancelAll(),
    )
    strategy = _StrategyThatMustNotStart()
    engine.register(strategy, symbols={"BTC"})

    with capture_events() as logs:
        exit_code = asyncio.run(asyncio.wait_for(engine.run(), timeout=5))

    assert exit_code == 0
    assert not strategy.started
    assert not strategy.snapshotted
    events = [log["event"] for log in logs]
    assert "engine.barrier_cleared" in events
    assert "engine.feed_started" not in events
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["job"] == "cancel_all"
    assert logs[-1]["outcome"] == "done"
    assert logs[-1]["exit_code"] == 0
    assert logs[-1]["left"] == "none"
