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
import os
import signal
from collections.abc import Callable
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
    InstrumentSpec,
    MarketTick,
    OrderDenied,
    OrderEvent,
    OrderFilled,
    OrderLive,
    OrderState,
    OrderType,
    PlaceSignal,
    Side,
    TimeInForce,
    derive_cloid,
)
from tickwright.engine.guard import RealGuard
from tickwright.engine.runner import Engine
from tickwright.observability.testing import capture_events
from tickwright.strategies import SingleShotLimitStrategy, SingleShotMarketStrategy

_ROWS: list[dict[str, str | int]] = [
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


async def _until(condition: Callable[[], bool]) -> None:
    """Spin the loop (bounded by the caller's ``wait_for``) until ``condition``
    holds — signal delivery is genuinely asynchronous, so the test waits for
    the observable effect rather than assuming a delivery order."""
    while not condition():
        await asyncio.sleep(0)


def test_sigterm_stops_the_engine_gracefully(tmp_path: Path) -> None:
    """The operator contract (ADR-0024): SIGTERM → graceful stop → exit 0."""
    ticks = _write_ticks(tmp_path / "ticks.jsonl")

    async def main() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        exchange = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
        bus.subscribe(MarketTick, exchange.on_tick)
        feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
        engine = Engine(bus=bus, clock=clock, store=store, exchange=exchange, feed=feed)

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(lambda: engine.state is ComponentState.RUNNING), timeout=5)
        os.kill(os.getpid(), signal.SIGTERM)
        return await asyncio.wait_for(run, timeout=5), engine

    exit_code, engine = asyncio.run(main())

    assert exit_code == 0
    assert engine.state is ComponentState.STOPPED


def test_sigusr1_trips_the_kill_switch_and_sigusr2_resets_it(tmp_path: Path) -> None:
    """The operator kill switch (ADR-0026): SIGUSR1 halts placements durably —
    subsequent ones are DENIED through the real guard — and SIGUSR2 re-enables."""
    spec = InstrumentSpec(
        symbol="BTC", sz_decimals=3, max_decimals=6, max_sig_figs=5, min_notional=Decimal("10")
    )

    def limit_signal(seq: int) -> PlaceSignal:
        return PlaceSignal(
            ts_event=1_000,
            ts_init=1_000,
            strategy_id="operator",
            symbol="BTC",
            seq=seq,
            side=Side.BUY,
            quantity=Decimal("0.5"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            price=Decimal("41000"),
        )

    async def main() -> None:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        venue = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
        bus.subscribe(MarketTick, venue.on_tick)
        feed = ReplayFeed(path=_write_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock)
        guard = RealGuard(specs={"BTC": spec}, store=store, clock=clock)
        engine = Engine(bus=bus, clock=clock, store=store, exchange=venue, feed=feed, guard=guard)

        outcomes: list[OrderEvent] = []
        ticks_seen = asyncio.Event()

        async def record(event: OrderEvent) -> None:
            outcomes.append(event)

        async def on_tick(_: MarketTick) -> None:
            ticks_seen.set()

        bus.subscribe(OrderEvent, record)
        bus.subscribe(MarketTick, on_tick)

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(lambda: engine.state is ComponentState.RUNNING), timeout=5)
        # The venue prices limits off the latest tick: wait for the replay to land.
        await asyncio.wait_for(ticks_seen.wait(), timeout=5)

        os.kill(os.getpid(), signal.SIGUSR1)
        await asyncio.wait_for(_until(lambda: guard.kill_switch_tripped), timeout=5)
        await bus.publish(limit_signal(seq=1))
        assert isinstance(outcomes[-1], OrderDenied)

        os.kill(os.getpid(), signal.SIGUSR2)
        await asyncio.wait_for(_until(lambda: not guard.kill_switch_tripped), timeout=5)
        await bus.publish(limit_signal(seq=2))
        assert isinstance(outcomes[-1], OrderLive)

        await engine.stop()
        assert await run == 0

    asyncio.run(main())


def test_invariant_violation_faults_the_engine_and_exits_nonzero(tmp_path: Path) -> None:
    """ADR-0014/0024 fail-fast: an ``InvariantViolation`` in an engine-internal
    handler pierces everything — siblings cancelled, ``FAULTED``, non-zero exit."""
    ticks = _write_ticks(tmp_path / "ticks.jsonl")

    async def faulted_life() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        exchange = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
        bus.subscribe(MarketTick, exchange.on_tick)
        feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
        # A real guard with no specs: the first placement is a composition-root
        # wiring bug (ADR-0031) and raises InvariantViolation inside the raw
        # ExecutionManager.on_signal handler — the engine must not contain it.
        guard = RealGuard(specs={}, store=store, clock=clock)
        engine = Engine(
            bus=bus, clock=clock, store=store, exchange=exchange, feed=feed, guard=guard
        )
        engine.register(
            SingleShotMarketStrategy(
                strategy_id="doomed", bus=bus, clock=clock, side=Side.BUY, quantity=Decimal("0.5")
            ),
            symbols={"BTC"},
        )
        return await engine.run(), engine

    with capture_events() as logs:
        exit_code, engine = asyncio.run(faulted_life())

    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED
    assert "engine.faulted" in [log["event"] for log in logs]


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
            dict(r, ts_event=int(r["ts_event"]) + 10_000, trade_id=f"2-{r['trade_id']}")
            for r in _ROWS
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
