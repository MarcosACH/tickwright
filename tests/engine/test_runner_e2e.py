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

from structlog.typing import EventDict

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
from tickwright.observability.testing import capture_events
from tickwright.strategies import SingleShotLimitStrategy, SingleShotMarketStrategy

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


async def _run_to_fill_then_stop(ticks: Path, db: Path) -> tuple[int, Engine]:
    """One supervised life: full real wiring, run until the fill, stop gracefully."""
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


def test_run_replays_trades_and_exits_zero_on_graceful_stop(tmp_path: Path) -> None:
    ticks = _write_ticks(tmp_path / "ticks.jsonl")
    db = tmp_path / "saga.db"

    exit_code, engine = asyncio.run(_run_to_fill_then_stop(ticks, db))

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


def test_named_events_prove_the_startup_order(tmp_path: Path) -> None:
    """ADR-0024's ordering, observable: the barrier clears before the feed
    starts, and nothing places before the barrier — inv 5 of ADR-0011."""
    ticks = _write_ticks(tmp_path / "ticks.jsonl")

    with capture_events() as logs:
        asyncio.run(_run_to_fill_then_stop(ticks, tmp_path / "saga.db"))

    names = [log["event"] for log in logs]
    barrier_at = names.index("engine.barrier_cleared")
    feed_at = names.index("engine.feed_started")
    assert barrier_at < feed_at
    placed_at = [i for i, name in enumerate(names) if name == "order.placed"]
    assert placed_at, "the run must actually place an order for the proof to bite"
    assert all(barrier_at < i for i in placed_at)

    # The runner owns the run-id correlation binding: every record after the
    # bind — the whole lifecycle — is traceable to this run (ADR-0020).
    assert _every_lifecycle_record_carries_a_run_id(logs)


def _every_lifecycle_record_carries_a_run_id(logs: list[EventDict]) -> bool:
    engine_records = [log for log in logs if str(log["event"]).startswith("engine.")]
    return bool(engine_records) and all(log.get("run_id") for log in engine_records)


def test_graceful_stop_leaves_resting_live_orders_for_the_next_start_to_re_adopt(
    tmp_path: Path,
) -> None:
    """ADR-0024: a graceful stop does not cancel resting ``LIVE`` orders —
    crash and graceful stop converge on the one snapshot-plus-reconcile path."""
    db = tmp_path / "saga.db"
    clock = ManualClock()
    cloid = derive_cloid("resting:BTC:1")

    async def first_life() -> PaperExchange:
        """Rest a BUY limit below the market, then stop gracefully."""
        bus = InMemoryBus()
        store = SQLiteStore(db)
        venue = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
        bus.subscribe(MarketTick, venue.on_tick)
        feed = ReplayFeed(path=_write_ticks(tmp_path / "first.jsonl"), bus=bus, clock=clock)
        engine = Engine(bus=bus, clock=clock, store=store, exchange=venue, feed=feed)
        engine.register(
            SingleShotLimitStrategy(
                strategy_id="resting",
                bus=bus,
                clock=clock,
                side=Side.BUY,
                quantity=Decimal("0.5"),
                price=Decimal("41000"),
            ),
            symbols={"BTC"},
        )

        live = asyncio.Event()

        async def on_order_event(event: OrderEvent) -> None:
            if event.cloid == cloid and event.state is OrderState.LIVE:
                live.set()

        bus.subscribe(OrderEvent, on_order_event)

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(live.wait(), timeout=5)
        await engine.stop()
        assert await run == 0
        return venue

    venue = asyncio.run(first_life())

    # The resting LIVE order survived the graceful stop, checkpointed, untouched.
    between = SQLiteStore(db)
    try:
        rested = between.get_order(cloid)
        assert rested is not None
        assert rested.state is OrderState.LIVE
    finally:
        between.close()

    async def second_life(venue: PaperExchange) -> tuple[int, list[OrderEvent]]:
        """Restart over the surviving store and venue: the barrier re-adopts.

        The venue outlives the process (its push link from the first life is
        dead, exactly as after a crash); only ``fetch_order`` venue truth and
        the durable store connect the two lives.
        """
        bus = InMemoryBus()
        store = SQLiteStore(db)
        # Fresh non-crossing ticks, timestamped after the first life — virtual
        # time never moves backward across lives on the shared clock.
        later = [
            dict(r, ts_event=r["ts_event"] + 10_000, trade_id=f"2-{r['trade_id']}") for r in _ROWS
        ]
        (tmp_path / "second.jsonl").write_text("\n".join(json.dumps(r) for r in later) + "\n")
        feed = ReplayFeed(path=tmp_path / "second.jsonl", bus=bus, clock=clock)
        engine = Engine(bus=bus, clock=clock, store=store, exchange=venue, feed=feed)
        engine.register(
            SingleShotLimitStrategy(
                strategy_id="resting",
                bus=bus,
                clock=clock,
                side=Side.BUY,
                quantity=Decimal("0.5"),
                price=Decimal("41000"),
            ),
            symbols={"BTC"},
        )

        events: list[OrderEvent] = []

        async def record(event: OrderEvent) -> None:
            events.append(event)

        bus.subscribe(OrderEvent, record)

        run = asyncio.create_task(engine.run())
        while engine.state is not ComponentState.RUNNING and not run.done():
            await asyncio.sleep(0)
        if run.done():
            return await run, events  # surfaces a startup failure instead of hanging
        await engine.stop()
        return await run, events

    exit_code, events = asyncio.run(second_life(venue))

    assert exit_code == 0
    # Re-adopted, not resolved away: the saga is still LIVE and nothing failed.
    assert not any(e.state is OrderState.FAILED for e in events)
    after = SQLiteStore(db)
    try:
        adopted = after.get_order(cloid)
        assert adopted is not None
        assert adopted.state is OrderState.LIVE
    finally:
        after.close()
    # The venue still holds exactly the one resting order — no duplicate send.
    view = asyncio.run(venue.fetch_order(cloid))
    assert view is not None and view.has_record
