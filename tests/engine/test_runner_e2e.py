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
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path

from kafka_fakes import FakeKafkaBroker
from ledgers import GENESIS, ledger
from structlog.typing import EventDict

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.bus.kafka import KafkaBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AccountSpec,
    ComponentState,
    InstrumentSpec,
    InvariantViolation,
    MarketTick,
    Order,
    OrderDenied,
    OrderEvent,
    OrderFilled,
    OrderLive,
    OrderState,
    OrderType,
    PlaceOrder,
    PlaceSignal,
    Side,
    TimeInForce,
    VenueOrderView,
    derive_cloid,
)
from tickwright.engine.guard import RealGuard
from tickwright.engine.runner import Engine, EngineConfig
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
    exchange = PaperExchange(
        bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
    )
    feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
    projection = ledger()
    engine = Engine(
        bus=bus, clock=clock, store=store, exchange=exchange, feed=feed, portfolio=projection
    )
    strategy = SingleShotMarketStrategy(
        strategy_id="trivial",
        bus=bus,
        clock=clock,
        portfolio=projection.for_strategy("trivial"),
        side=Side.BUY,
        quantity=Decimal("0.5"),
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
        exchange = PaperExchange(
            bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
        )
        feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
        engine = Engine(
            bus=bus, clock=clock, store=store, exchange=exchange, feed=feed, portfolio=ledger()
        )

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
        venue = PaperExchange(
            bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
        )
        feed = ReplayFeed(path=_write_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock)
        guard = RealGuard(specs={"BTC": spec}, store=store, clock=clock)
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=venue,
            feed=feed,
            guard=guard,
            portfolio=ledger(),
        )

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


class _HangingFeed:
    """A feed whose ``stop()`` never returns — a wedged venue connection at
    teardown. A ``MarketFeed`` double at the venue boundary."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await asyncio.Event().wait()


class _BlockingFeed:
    """A feed whose read loop runs until cancelled — a live feed that never
    reaches end-of-file, unlike a replay. A ``MarketFeed`` double at the venue
    boundary; ``started`` fires once the loop is actually running."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def start(self) -> None:
        self.started.set()
        await asyncio.Event().wait()

    async def stop(self) -> None:
        return None


def test_shutdown_is_bounded_a_hung_teardown_faults_instead_of_hanging(tmp_path: Path) -> None:
    """ADR-0024: the reverse shutdown is bounded by ``shutdown_timeout`` — a
    teardown that cannot finish must fault non-zero, never wedge the process."""

    async def main() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        exchange = PaperExchange(
            bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
        )
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=exchange,
            feed=_HangingFeed(),
            config=EngineConfig(shutdown_timeout_seconds=0.05),
            portfolio=ledger(),
        )
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(lambda: engine.state is ComponentState.RUNNING), timeout=5)
        await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5), engine

    exit_code, engine = asyncio.run(main())

    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED


def test_graceful_stop_cancels_a_still_running_feed(tmp_path: Path) -> None:
    """ADR-0024 reverse shutdown: a graceful stop of a feed still mid-read
    cancels the feed task and exits 0 — the live-feed path that a replay hitting
    end-of-file never exercises. The feed's ``start()`` never returns, so exit 0
    is only reachable if the reverse shutdown cancelled the task; otherwise the
    ``TaskGroup`` would wait on it forever and the run would time out."""

    async def main() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        exchange = PaperExchange(
            bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
        )
        feed = _BlockingFeed()
        engine = Engine(
            bus=bus, clock=clock, store=store, exchange=exchange, feed=feed, portfolio=ledger()
        )

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(lambda: engine.state is ComponentState.RUNNING), timeout=5)
        await asyncio.wait_for(feed.started.wait(), timeout=5)
        await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5), engine

    exit_code, engine = asyncio.run(main())

    assert exit_code == 0
    assert engine.state is ComponentState.STOPPED


def test_invariant_violation_faults_the_engine_and_exits_nonzero(tmp_path: Path) -> None:
    """ADR-0014/0024 fail-fast: an ``InvariantViolation`` in an engine-internal
    handler pierces everything — siblings cancelled, ``FAULTED``, non-zero exit."""
    ticks = _write_ticks(tmp_path / "ticks.jsonl")

    async def faulted_life() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        exchange = PaperExchange(
            bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
        )
        feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
        # A real guard with no specs: the first placement is a composition-root
        # wiring bug (ADR-0031) and raises InvariantViolation inside the raw
        # ExecutionManager.on_signal handler — the engine must not contain it.
        guard = RealGuard(specs={}, store=store, clock=clock)
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=exchange,
            feed=feed,
            guard=guard,
            portfolio=ledger(),
        )
        engine.register(
            SingleShotMarketStrategy(
                strategy_id="doomed",
                bus=bus,
                clock=clock,
                portfolio=ledger().for_strategy("doomed"),
                side=Side.BUY,
                quantity=Decimal("0.5"),
            ),
            symbols={"BTC"},
        )
        return await engine.run(), engine

    with capture_events() as logs:
        exit_code, engine = asyncio.run(faulted_life())

    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED
    assert "engine.faulted" in [log["event"] for log in logs]


def test_a_broken_stop_hook_on_the_fault_path_is_recorded_not_swallowed(tmp_path: Path) -> None:
    """ADR-0020/0024: the faulted teardown is best-effort but not silent. A stop
    hook that raises mid-fault (here the store failing to close) is recorded as
    ``engine.stop_hook_failed`` and cannot mask the fault or block the non-zero
    exit — the operator sees the lost resource in the same run's trail."""

    class _StoreThatBreaksOnClose(SQLiteStore):
        def close(self) -> None:
            raise RuntimeError("store close broke during fault teardown")

    async def faulted_life() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = _StoreThatBreaksOnClose(tmp_path / "saga.db")
        exchange = PaperExchange(
            bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
        )
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=exchange,
            feed=_PoisonedFeed(),
            portfolio=ledger(),
        )
        return await engine.run(), engine

    with capture_events() as logs:
        exit_code, engine = asyncio.run(faulted_life())

    names = [log["event"] for log in logs]
    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED
    # The fault is not masked by the broken hook, and the break is on the record.
    assert "engine.faulted" in names
    hook_failures = [log for log in logs if log["event"] == "engine.stop_hook_failed"]
    assert len(hook_failures) == 1
    assert hook_failures[0]["hook"] == "store.close"


class _PoisonedFeed:
    """A feed whose read loop breaks an engine assumption — the fail-fast class.
    A ``MarketFeed`` double at the venue boundary."""

    async def start(self) -> None:
        raise InvariantViolation("the read loop broke an engine assumption")

    async def stop(self) -> None:
        return None


def _kafka_bus(broker: FakeKafkaBroker) -> KafkaBus:
    return KafkaBus(
        bootstrap_servers="kafka:9092",
        topic="tickwright.events",
        group_id="tickwright",
        producer_factory=broker.producer,
        consumer_factory=broker.consumer,
    )


def test_the_runner_owns_the_bus_lifecycle_connect_on_start_disconnect_on_stop(
    tmp_path: Path,
) -> None:
    """ADR-0024 steps 3 and the reverse shutdown: the runner starts the bus
    (Kafka: connect producer/consumer) and closes it on a graceful stop —
    observed at the process boundary, where the broker sees its clients."""
    broker = FakeKafkaBroker()

    async def main() -> tuple[int, Engine]:
        bus = _kafka_bus(broker)
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        exchange = PaperExchange(
            bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
        )
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=exchange,
            feed=_BlockingFeed(),
            portfolio=ledger(),
        )

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(lambda: engine.state is ComponentState.RUNNING), timeout=5)
        # Mid-run the bus is connected: the broker handed out started clients.
        assert [p.started for p in broker.producers] == [True]
        assert [c.started for c in broker.consumers] == [True]
        await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5), engine

    exit_code, engine = asyncio.run(main())

    assert exit_code == 0
    assert engine.state is ComponentState.STOPPED
    # The reverse shutdown closed the bus: every client disconnected.
    assert [p.started for p in broker.producers] == [False]
    assert [c.started for c in broker.consumers] == [False]


class _FaultingFeedThatRecordsStop:
    """A feed whose read loop faults the engine, recording whether teardown
    still stopped it — the fault path must cut the venue connection too.
    A ``MarketFeed`` double at the venue boundary."""

    def __init__(self) -> None:
        self.stopped = False

    async def start(self) -> None:
        raise InvariantViolation("the read loop broke an engine assumption")

    async def stop(self) -> None:
        self.stopped = True


def test_the_fault_path_walks_the_same_teardown_feed_stopped_and_bus_closed(
    tmp_path: Path,
) -> None:
    """The faulted teardown shares membership and order with the graceful one,
    differing only in failure policy (ADR-0024): a fault must still stop the
    feed (a live WS must not leak) and close the bus (a Kafka producer must
    flush — buffered writes survive the fault)."""
    broker = FakeKafkaBroker()
    feed = _FaultingFeedThatRecordsStop()

    async def faulted_life() -> tuple[int, Engine]:
        bus = _kafka_bus(broker)
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        exchange = PaperExchange(
            bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
        )
        engine = Engine(
            bus=bus, clock=clock, store=store, exchange=exchange, feed=feed, portfolio=ledger()
        )
        return await engine.run(), engine

    exit_code, engine = asyncio.run(faulted_life())

    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED
    assert feed.stopped, "the fault path must stop the feed, not just cancel its task"
    assert [p.started for p in broker.producers] == [False]
    assert [c.started for c in broker.consumers] == [False]


class _LifecycleRecordingVenue:
    """An ``Exchange`` that records the runner driving its lifecycle verbs, and
    what the rest of the process had already done by then. A network boundary is
    the one place a test double is allowed."""

    def __init__(self, timeline: list[str], broker: FakeKafkaBroker | None = None) -> None:
        self._timeline = timeline
        self._broker = broker
        self.bus_connected_at_start = False

    async def start(self) -> None:
        if self._broker is not None:
            # Observed at the process boundary: the broker has handed out
            # started clients, so the bus this venue reports on is already up.
            self.bus_connected_at_start = bool(self._broker.producers) and all(
                producer.started for producer in self._broker.producers
            )
        self._timeline.append("exchange.start")

    async def stop(self) -> None:
        self._timeline.append("exchange.stop")

    async def place(self, order: PlaceOrder) -> None:
        raise AssertionError("this run never places")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("this run never cancels")

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        self._timeline.append("venue.read")
        return VenueOrderView(status=None)

    def account_spec(self) -> AccountSpec:
        return AccountSpec(account_id="paper-default", genesis_collateral=_GENESIS)

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        return {}


def _submitted_saga(cloid: str) -> Order:
    """A saga the barrier must ask the venue about — without one, the mass
    rebuild has nothing to read and the ordering proof has no venue read to
    stand on."""
    order = Order(
        cloid=cloid,
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.LIMIT,
    )
    order.state = OrderState.SUBMITTED
    return order


def test_the_runner_starts_the_exchange_after_the_bus_and_before_the_barrier(
    tmp_path: Path,
) -> None:
    """ADR-0024 step 4: the ``Exchange``'s connect half runs after the bus is up
    — nothing can publish before that — and *before* the startup barrier, so a
    venue refusal precedes any order and the barrier observes an aligned venue."""
    broker = FakeKafkaBroker()
    timeline: list[str] = []
    venue = _LifecycleRecordingVenue(timeline, broker)

    async def main() -> int:
        store = SQLiteStore(tmp_path / "saga.db")
        store.checkpoint(_submitted_saga("0xabc"), ts_ns=500)
        engine = Engine(
            bus=_kafka_bus(broker),
            clock=ManualClock(),
            store=store,
            exchange=venue,
            feed=_BlockingFeed(),
            portfolio=ledger(),
        )
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(lambda: engine.state is ComponentState.RUNNING), timeout=5)
        await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5)

    assert asyncio.run(main()) == 0

    assert venue.bus_connected_at_start, "the bus must be up before the venue is connected"
    assert "venue.read" in timeline, "the barrier must actually read for the proof to bite"
    assert timeline.index("exchange.start") < timeline.index("venue.read")


class _TimelineFeed:
    """A live-shaped feed — its read loop runs until cancelled — that records
    the runner cutting it, so teardown order is observable at the seam. A
    ``MarketFeed`` double at the venue boundary."""

    def __init__(self, timeline: list[str]) -> None:
        self._timeline = timeline
        self.started = asyncio.Event()

    async def start(self) -> None:
        self.started.set()
        await asyncio.Event().wait()

    async def stop(self) -> None:
        self._timeline.append("feed.stop")


class _TimelineStore(SQLiteStore):
    """The real store, recording the one teardown step it owns — the last."""

    def __init__(self, path: Path, timeline: list[str]) -> None:
        super().__init__(path)
        self._timeline = timeline

    def close(self) -> None:
        self._timeline.append("store.close")
        super().close()


def test_the_reverse_shutdown_stops_the_exchange_immediately_after_the_feed(
    tmp_path: Path,
) -> None:
    """ADR-0024's reverse shutdown: ``exchange.stop`` sits directly after
    ``feed.stop`` in the one ordered membership, so whatever the adapter runs
    is stopped before the bus drains and the store closes behind it."""
    timeline: list[str] = []

    async def main() -> int:
        feed = _TimelineFeed(timeline)
        engine = Engine(
            bus=InMemoryBus(),
            clock=ManualClock(),
            store=_TimelineStore(tmp_path / "saga.db", timeline),
            exchange=_LifecycleRecordingVenue(timeline),
            feed=feed,
            portfolio=ledger(),
        )
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(feed.started.wait(), timeout=5)
        await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5)

    assert asyncio.run(main()) == 0

    assert timeline == ["exchange.start", "feed.stop", "exchange.stop", "store.close"]


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
        venue = PaperExchange(
            bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
        )
        feed = ReplayFeed(path=_write_ticks(tmp_path / "first.jsonl"), bus=bus, clock=clock)
        engine = Engine(
            bus=bus, clock=clock, store=store, exchange=venue, feed=feed, portfolio=ledger()
        )
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
        engine = Engine(
            bus=bus, clock=clock, store=store, exchange=venue, feed=feed, portfolio=ledger()
        )
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
