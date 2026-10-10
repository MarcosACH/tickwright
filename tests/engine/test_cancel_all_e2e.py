"""Operator cancel all (ADR-0052, ADR-0060): the ``Engine`` as a one-shot exit run.

The real paper path, with no strategy: ``PaperExchange`` over ``InMemoryBus`` and
``SQLiteStore``, on ``ManualClock``. The run boots and reconciles like a normal
run, then does its one job and exits with the ADR-0060 code.
"""

import asyncio
import json
import os
import signal
from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path

import pytest
from ledgers import GENESIS, checkpointer
from structlog.typing import EventDict
from venue_doubles import VenueLink

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AggressorSide,
    Exchange,
    MarketTick,
    Order,
    OrderEvent,
    OrderRef,
    OrderState,
    OrderType,
    PlaceOrder,
    Side,
    TimeInForce,
    VenueOpenOrder,
    VenueReadFailure,
    derive_cloid,
)
from tickwright.engine.checkpoint import Checkpointer
from tickwright.engine.exit_run import OperatorCancelAll
from tickwright.engine.guard import RealGuard
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


def test_an_exit_run_on_a_store_another_engine_holds_exits_two_and_writes_nothing(
    tmp_path: Path,
) -> None:
    db = tmp_path / "saga.db"
    holder = SQLiteStore(db)
    assert holder.lock() is None
    bus = InMemoryBus()
    clock = ManualClock()
    engine = Engine(
        bus=bus,
        clock=clock,
        store=SQLiteStore(db),
        exchange=PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
            applied_fills=lambda cloid: (),
        ),
        feed=ReplayFeed(path=_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock),
        exit_job=OperatorCancelAll(),
    )

    with capture_events() as logs:
        exit_code = asyncio.run(asyncio.wait_for(engine.run(), timeout=5))

    # Code 2 is "never booted": nothing moved, so the operator fixes the lock
    # and runs it again (ADR-0060).
    assert exit_code == 2
    assert [log["event"] for log in logs] == ["exit.refused"]
    assert logs[0]["reason"] == "lock_held"
    assert logs[0]["store"] == str(db)
    assert f"Process {os.getpid()} holds" in logs[0]["holder"]
    try:
        assert holder.load_account() is None
    finally:
        holder.close()


class _ReadThatNeverAnswers(VenueLink):
    """The account read hangs, as on a venue that stopped answering."""

    def __init__(self, venue: PaperExchange) -> None:
        super().__init__(venue)
        self.reading = asyncio.Event()

    async def fetch_open_orders(self) -> list[VenueOpenOrder] | VenueReadFailure:
        self.reading.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


def test_sigterm_during_cancel_all_stops_the_run_with_exit_one(tmp_path: Path) -> None:
    async def go() -> int:
        bus = InMemoryBus()
        clock = ManualClock()
        venue = _ReadThatNeverAnswers(
            PaperExchange(
                bus=bus,
                clock=clock,
                fill_model=ImmediateFillModel(),
                genesis_collateral=GENESIS,
                account_net=dict,
                applied_fills=lambda cloid: (),
            )
        )
        engine = Engine(
            bus=bus,
            clock=clock,
            store=SQLiteStore(tmp_path / "saga.db"),
            exchange=venue,
            feed=ReplayFeed(path=_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock),
            exit_job=OperatorCancelAll(),
        )
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(venue.reading.wait(), timeout=5)
        os.kill(os.getpid(), signal.SIGTERM)
        return await asyncio.wait_for(run, timeout=5)

    with capture_events() as logs:
        exit_code = asyncio.run(go())

    # The job never said the venue is clear, so the run is not done (ADR-0060).
    assert exit_code == 1
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "stopped"
    assert logs[-1]["exit_code"] == 1


async def _rest(exchange: PaperExchange, sagas: Checkpointer, *, seq: int, price: str) -> str:
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
            price=Decimal(price),
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


def _trade(price: str, *, aggressor: AggressorSide, ts: int) -> MarketTick:
    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol="BTC",
        price=Decimal(price),
        size=Decimal("1"),
        aggressor_side=aggressor,
        trade_id=f"t{ts}",
        seq=0,
    )


def _cancel_all(
    tmp_path: Path,
    *,
    prices: Sequence[str],
    link: Callable[[PaperExchange, InMemoryBus], Exchange] | None = None,
    kill_switch: bool | None = None,
) -> tuple[int, list[str], list[EventDict]]:
    """Rest one BUY per price on paper, then run cancel all over it.

    ``link`` puts a venue double in front of paper. ``kill_switch`` stores that
    state before the run and gives the engine a real guard to restore it.
    Returns the exit code, the cloids in price order, and every event the run
    emitted.
    """
    bus = InMemoryBus()
    clock = ManualClock()
    store = SQLiteStore(tmp_path / "saga.db")
    paper = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
        applied_fills=lambda cloid: (),
    )

    async def go() -> tuple[int, list[str]]:
        # Paper prices a LIMIT against the last tick, so it needs one first.
        await bus.publish(_trade("42000", aggressor=AggressorSide.BUY, ts=500))
        # The engine's own write path opens the ledger row a store with orders needs.
        sagas = checkpointer(store, clock=clock)
        sagas.recover()
        cloids = [
            await _rest(paper, sagas, seq=seq, price=price)
            for seq, price in enumerate(prices, start=1)
        ]
        guard = None
        if kill_switch is not None:
            store.save_kill_switch(tripped=kill_switch, reason="operator halt", ts_ns=0)
            guard = RealGuard(specs=paper.instrument_specs(), store=store, clock=clock)
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=paper if link is None else link(paper, bus),
            feed=ReplayFeed(path=_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock),
            guard=guard,
            exit_job=OperatorCancelAll(),
        )
        return await asyncio.wait_for(engine.run(), timeout=5), cloids

    with capture_events() as logs:
        exit_code, cloids = asyncio.run(go())
    return exit_code, cloids, logs


def _sagas(tmp_path: Path, cloids: Sequence[str]) -> list[Order]:
    """Each saga as the run left it in the store."""
    store = SQLiteStore(tmp_path / "saga.db")
    try:
        orders = [store.get_order(cloid) for cloid in cloids]
    finally:
        store.close()
    assert all(order is not None for order in orders)
    return [order for order in orders if order is not None]


def test_an_exit_run_cancels_every_resting_order_and_leaves_none(tmp_path: Path) -> None:
    exit_code, cloids, logs = _cancel_all(tmp_path, prices=["30000", "30000", "30000"])

    assert exit_code == 0
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["left"] == "none"
    for order in _sagas(tmp_path, cloids):
        assert order.state is OrderState.CANCELLED
        # The operator's intent was durable before the send, so a crash
        # between the two never lets reconcile call the order a ghost.
        assert order.cancel_requested


@pytest.mark.parametrize("tripped", [True, False])
def test_cancel_all_leaves_the_kill_switch_as_it_found_it(tmp_path: Path, tripped: bool) -> None:
    exit_code, cloids, logs = _cancel_all(tmp_path, prices=["30000"], kill_switch=tripped)

    # A tripped switch halts new orders only. The cancels still go out.
    assert exit_code == 0
    assert [order.state for order in _sagas(tmp_path, cloids)] == [OrderState.CANCELLED]
    events = [log["event"] for log in logs]
    assert "guard.kill_switch_tripped" not in events
    assert "guard.kill_switch_reset" not in events
    store = SQLiteStore(tmp_path / "saga.db")
    try:
        state = store.load_kill_switch()
    finally:
        store.close()
    assert state is not None
    assert state.tripped is tripped
    assert state.reason == "operator halt"


class _FillsBeforeTheCancel(VenueLink):
    """The market trades through one order after the read and before the cancel.

    So the cancel reaches the venue for an order that is already gone. A live
    venue answers that with an error status (ADR-0060). Paper ignores it.
    """

    def __init__(self, venue: PaperExchange, bus: InMemoryBus) -> None:
        super().__init__(venue)
        self._bus = bus

    async def cancel(self, refs: Sequence[OrderRef]) -> None:
        await self._bus.publish(_trade("34000", aggressor=AggressorSide.SELL, ts=900))
        await super().cancel(refs)


def test_an_order_already_gone_at_the_venue_does_not_fail_the_run(tmp_path: Path) -> None:
    exit_code, cloids, logs = _cancel_all(
        tmp_path, prices=["35000", "30000", "30000"], link=_FillsBeforeTheCancel
    )

    assert exit_code == 0
    assert logs[-1]["left"] == "none"
    filled, *cancelled = _sagas(tmp_path, cloids)
    assert filled.state is OrderState.FILLED
    assert [order.state for order in cancelled] == [OrderState.CANCELLED] * 2


_STUCK = derive_cloid("resting:BTC:1")
"""The first order ``_cancel_all`` rests, by the cloid ``_rest`` derives for it."""


class _LosesEveryCancelForOne(VenueLink):
    """Every cancel for ``_STUCK`` is lost on the way, so it stays resting."""

    def __init__(self, venue: PaperExchange, bus: InMemoryBus) -> None:
        super().__init__(venue)
        self.sends_for_stuck = 0

    async def cancel(self, refs: Sequence[OrderRef]) -> None:
        self.sends_for_stuck += sum(ref.cloid == _STUCK for ref in refs)
        await super().cancel([ref for ref in refs if ref.cloid != _STUCK])


def test_an_order_resting_through_both_cancels_stops_the_run_with_exit_one(
    tmp_path: Path,
) -> None:
    links: list[_LosesEveryCancelForOne] = []

    def link(paper: PaperExchange, bus: InMemoryBus) -> Exchange:
        links.append(_LosesEveryCancelForOne(paper, bus))
        return links[-1]

    exit_code, cloids, logs = _cancel_all(tmp_path, prices=["30000", "30000", "30000"], link=link)

    assert exit_code == 1
    # One cancel, then one more for what the second read still showed.
    assert links[0].sends_for_stuck == 2
    remain = [log for log in logs if log["event"] == "cancel_all.orders_remain"]
    assert [log["orders"] for log in remain] == [_STUCK]
    assert logs[-1]["event"] == "exit.finished"
    assert logs[-1]["outcome"] == "stopped"
    assert logs[-1]["left"] == _STUCK
    stuck, *cancelled = _sagas(tmp_path, cloids)
    assert stuck.state is OrderState.LIVE
    assert [order.state for order in cancelled] == [OrderState.CANCELLED] * 2
