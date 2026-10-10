"""Operator cancel all (ADR-0052, ADR-0060): the ``Engine`` as a one-shot exit run.

The real paper path, with no strategy: ``PaperExchange`` over ``InMemoryBus`` and
``SQLiteStore``, on ``ManualClock``. The run boots and reconciles like a normal
run, then does its one job and exits with the ADR-0060 code.
"""

import asyncio
import json
from decimal import Decimal
from pathlib import Path

from ledgers import GENESIS, checkpointer

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AggressorSide,
    MarketTick,
    Order,
    OrderEvent,
    OrderState,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
    derive_cloid,
)
from tickwright.engine.checkpoint import Checkpointer
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


async def _rest(exchange: PaperExchange, sagas: Checkpointer, *, seq: int) -> str:
    """One engine order resting on paper, with its saga in the store.

    Seeded through the two seams rather than by an earlier engine run. Paper keeps
    its book in memory only (#484), so the exit run must share this exchange, and
    two engines cannot share one bus. The saga is left ``SUBMITTED``, as after a
    crash before the ack. The boot barrier then heals it to ``LIVE`` from paper's
    own record, so it rests when the job starts.
    """
    signal_id = f"resting:BTC:{seq}"
    cloid = derive_cloid(signal_id)
    await exchange.place(
        PlaceOrder(
            cloid=cloid,
            symbol="BTC",
            side=Side.BUY,
            quantity=Decimal("0.1"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            price=Decimal("30000"),
        )
    )
    sagas.checkpoint(
        Order(
            cloid=cloid,
            strategy_id="resting",
            signal_id=signal_id,
            symbol="BTC",
            side=Side.BUY,
            quantity=Decimal("0.1"),
            order_type=OrderType.LIMIT,
            state=OrderState.SUBMITTED,
            created_ts_ns=0,
        )
    )
    return cloid


def test_an_exit_run_cancels_every_resting_order_and_leaves_none(tmp_path: Path) -> None:
    bus = InMemoryBus()
    clock = ManualClock()
    db = tmp_path / "saga.db"
    store = SQLiteStore(db)
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
        applied_fills=lambda cloid: (),
    )

    async def go() -> tuple[int, list[str]]:
        # Paper prices a LIMIT against the last tick, so it needs one first.
        await bus.publish(
            MarketTick(
                ts_event=500,
                ts_init=500,
                symbol="BTC",
                price=Decimal("42000"),
                size=Decimal("1"),
                aggressor_side=AggressorSide.BUY,
                trade_id="seed",
                seq=0,
            )
        )
        # The engine's own write path opens the ledger row a store with orders needs.
        sagas = checkpointer(store, clock=clock)
        sagas.recover()
        cloids = [await _rest(exchange, sagas, seq=seq) for seq in (1, 2, 3)]
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=exchange,
            feed=ReplayFeed(path=_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock),
            exit_job=OperatorCancelAll(),
        )
        return await asyncio.wait_for(engine.run(), timeout=5), cloids

    with capture_events() as logs:
        exit_code, cloids = asyncio.run(go())

    assert exit_code == 0
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["left"] == "none"
    reopened = SQLiteStore(db)
    try:
        for cloid in cloids:
            order = reopened.get_order(cloid)
            assert order is not None
            assert order.state is OrderState.CANCELLED
            # The operator's intent was durable before the send, so a crash
            # between the two never lets reconcile call the order a ghost.
            assert order.cancel_requested
    finally:
        reopened.close()
