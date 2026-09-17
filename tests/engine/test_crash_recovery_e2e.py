"""Crash-recovery E2E (issue #14): kill after the ``PENDING`` checkpoint, restart,
and converge — through the whole real pipeline.

First life is the real wiring — ``ReplayFeed`` tick → ``SingleShotLimitStrategy``
→ ``ExecutionManager`` → venue — killed inside the send window by a transport
that dies mid-``place`` (the one crash ADR-0008 calls irreducible). The venue is
a real ``PaperExchange`` playing the remote venue: its report link (its bus) and
any acks on it die with the process; its own state survives, exactly like a real
venue outliving our crash. The second life rebuilds the ``Cache`` from the
surviving ``Store``, runs the startup barrier, and must end with **no duplicate
placement for the cloid and no lost fill** — with an at-least-once redelivery of
the original signal thrown in.
"""

import asyncio
import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from kafka_fakes import FakeKafkaBroker, Record
from ledgers import GENESIS, checkpointer
from store_backends import (
    STORE_BACKEND_PARAMS,
    PostgresBackend,
    SQLiteBackend,
    resolve_backend,
)
from venue_doubles import VenueLink

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.bus.kafka import KafkaBus
from tickwright.adapters.bus.serde import decode_event
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.domain import (
    AggressorSide,
    ExecutionReport,
    FillReport,
    MarketTick,
    OrderEvent,
    OrderFailed,
    OrderFilled,
    OrderPlaced,
    OrderRef,
    OrderState,
    OrderSubmitted,
    OrderType,
    PlaceOrder,
    PlaceSignal,
    Side,
    Signal,
    Store,
    TimeInForce,
    VenueOrderView,
    VenueReadFailure,
    derive_cloid,
)
from tickwright.engine.barrier import StartupBarrier
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.reconcile import ReconcileConfig, Reconciler
from tickwright.engine.strategy_host import StrategyHost
from tickwright.strategies import SingleShotLimitStrategy

_CLOID = derive_cloid("trivial:BTC:1")

# The store the crash leaves behind, and the store the second life reopens over
# the same durable backing — one per backend. Postgres auto-skips without a
# reachable server (see ``store_backends``), so the default run stays hermetic.
Backend = SQLiteBackend | PostgresBackend


@pytest.fixture(params=STORE_BACKEND_PARAMS)
def store_backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Backend]:
    yield resolve_backend(request.param, tmp_path / "saga.db")


class _CrashingTransport(VenueLink):
    """The venue link for the doomed first life: the process dies in the send
    window. ``pre_send=True`` crashes before the request reaches the venue;
    otherwise the venue receives it and only the ack is lost (ADR-0008's
    irreducible window). A real network boundary is the one place a test double
    is allowed."""

    def __init__(self, venue: PaperExchange, *, pre_send: bool) -> None:
        super().__init__(venue)
        self._pre_send = pre_send

    async def place(self, order: PlaceOrder) -> None:
        if self._pre_send:
            raise ConnectionError("process died before the send left the box")
        await self._venue.place(order)
        raise ConnectionError("process died with the send in flight; ack lost")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("first life never cancels")

    async def fetch_order(self, ref: OrderRef) -> VenueOrderView | VenueReadFailure:
        raise AssertionError("first life never fetches")


def _ticks_file(path: Path, prices: list[str]) -> Path:
    rows = [
        {
            "symbol": "BTC",
            "price": price,
            "size": "3",
            "aggressor_side": "sell",
            "trade_id": chr(ord("a") + i),
            "ts_event": 1_000 * (i + 1),
        }
        for i, price in enumerate(prices)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _crossing_tick(ts: int = 5_000) -> MarketTick:
    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol="BTC",
        price=Decimal("40000"),
        size=Decimal("3"),
        aggressor_side=AggressorSide.SELL,
        trade_id="dead-window",
        seq=9,
    )


def _redelivered_signal() -> PlaceSignal:
    """The strategy's original signal, redelivered across the restart."""
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=1,
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("41000"),
    )


def _first_life(
    tmp_path: Path, backend: Backend, *, pre_send: bool
) -> tuple[PaperExchange, ManualClock]:
    """Run the real pipeline into the crash; return what survives it.

    The saga store is checkpointed and then closed — the crash. Its durable
    backing (a SQLite file, or the Postgres server) outlives the process, so the
    second life reopens it. The venue is the remote that survives our death.
    """
    bus = InMemoryBus()
    clock = ManualClock()
    store = backend.open()
    venue_bus = InMemoryBus()  # the venue's report link — dies with the process
    venue = PaperExchange(
        bus=venue_bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
    )
    checks = checkpointer(store, clock=clock)
    # Recovered before the feed starts, exactly as the runner starts a process
    # (ADR-0043 §6): this is where paper's genesis row is written, so a life that
    # skipped it would checkpoint its saga against a ledger that was never opened
    # — order history with no account row behind it, the one shape #188 refuses
    # outright. The crash below must leave a store the *runner* could have left.
    checks.recover()
    manager = ExecutionManager(
        bus=bus,
        exchange=_CrashingTransport(venue, pre_send=pre_send),
        checkpointer=checks,
    )
    strategy = SingleShotLimitStrategy(
        strategy_id="trivial",
        bus=bus,
        clock=clock,
        side=Side.BUY,
        quantity=Decimal("0.5"),
        price=Decimal("41000"),
    )
    feed = ReplayFeed(path=_ticks_file(tmp_path / "ticks.jsonl", ["42000"]), bus=bus, clock=clock)

    bus.subscribe(MarketTick, strategy.on_tick)
    # This crash sim deliberately splits the venue's buses: it *publishes*
    # reports on the dead ``venue_bus`` (so they never reach the engine — the
    # severed post-send link) while *reading* ticks from the live engine bus.
    # That read/write split is the one case the venue's own tick subscription
    # (bound to its construction bus, ``venue_bus``) cannot cover, so the tick
    # wire to the engine bus stays explicit here.
    bus.subscribe(MarketTick, venue.on_tick)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)

    with pytest.raises(ConnectionError):
        asyncio.run(feed.run())

    # The write-ahead intent is the durable truth the crash left behind.
    record = store.get_order(_CLOID)
    assert record is not None and record.state is OrderState.PENDING
    store.close()  # the crash: the process, and its store connection, die
    return venue, clock


def _second_life(
    backend: Backend, venue: PaperExchange, clock: ManualClock
) -> tuple[InMemoryBus, Reconciler, list[OrderEvent], Store]:
    """Recovery wiring over the survivors: a store reopened on the same durable
    backing, both read-models restored from it, a fresh bus and manager.

    Restored through ``recover()`` rather than by rebuilding the ``Cache`` alone,
    because the ordering of the two is the ``Checkpointer``'s own rule (ADR-0043
    §6/§10) and this is the suite that most resembles the restart it governs.
    Reaching past it for ``cache.rebuild()`` exercised half the verb and skipped
    the half that can refuse the store — so the refusal had no crash-shaped case
    behind it, and this wiring could drift from the runner's without a red test.
    """
    bus = InMemoryBus()
    store = backend.open()
    checks = checkpointer(store, clock=clock)
    checks.recover()
    cache = checks.cache
    manager = ExecutionManager(
        bus=bus,
        exchange=venue,
        checkpointer=checks,
    )
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    events: list[OrderEvent] = []

    async def record(event: OrderEvent) -> None:
        events.append(event)

    bus.subscribe(OrderEvent, record)
    reconciler = Reconciler(
        bus=bus, clock=clock, exchange=venue, cache=cache, config=ReconcileConfig()
    )
    return bus, reconciler, events, store


def test_post_send_kill_recovers_the_landed_order_and_its_fill(
    tmp_path: Path, store_backend: Backend
) -> None:
    venue, clock = _first_life(tmp_path, store_backend, pre_send=False)

    # While we are dead the market crosses the resting BUY at the venue: it
    # fills, reporting into the dead link.
    asyncio.run(venue.on_tick(_crossing_tick()))

    bus, reconciler, events, store = _second_life(store_backend, venue, clock)
    asyncio.run(
        StartupBarrier(clock=clock, steps=(reconciler.reconcile_startup,)).run(timeout_seconds=30.0)
    )

    # No lost fill: the saga converged on the venue's executed truth.
    recovered = store.get_order(_CLOID)
    assert recovered is not None
    assert recovered.state is OrderState.FILLED
    assert recovered.cum_qty == Decimal("0.5")
    assert [type(ev) for ev in events] == [OrderSubmitted, OrderFilled]
    assert all(ev.reconciliation for ev in events)

    # At-least-once redelivery of the original signal: no duplicate placement.
    asyncio.run(bus.publish(_redelivered_signal()))
    view = asyncio.run(venue.fetch_order(OrderRef(cloid=_CLOID, symbol="BTC")))
    assert isinstance(view, VenueOrderView)
    assert [fill.trade_id for fill in view.fills] == [f"{_CLOID}-1"]
    assert [type(ev) for ev in events] == [OrderSubmitted, OrderFilled]
    store.close()


def test_pre_send_kill_resolves_the_unlanded_intent_failed_never_resent(
    tmp_path: Path, store_backend: Backend
) -> None:
    venue, clock = _first_life(tmp_path, store_backend, pre_send=True)

    bus, reconciler, events, store = _second_life(store_backend, venue, clock)
    asyncio.run(
        StartupBarrier(clock=clock, steps=(reconciler.reconcile_startup,)).run(timeout_seconds=30.0)
    )

    # The venue never saw the cloid: proven never-landed → FAILED (ADR-0010).
    recovered = store.get_order(_CLOID)
    assert recovered is not None
    assert recovered.state is OrderState.FAILED
    assert [type(ev) for ev in events] == [OrderFailed]

    # Redelivery converges on the terminal saga — nothing placed, ever.
    asyncio.run(bus.publish(_redelivered_signal()))
    assert [type(ev) for ev in events] == [OrderFailed]
    assert not [ev for ev in events if isinstance(ev, OrderPlaced)]
    view = asyncio.run(venue.fetch_order(OrderRef(cloid=_CLOID, symbol="BTC")))
    assert isinstance(view, VenueOrderView) and not view.has_record
    store.close()


def _fill_report(trade_id: str) -> FillReport:
    """The venue's fill for the resting BUY, as the adapter reports it."""
    return FillReport(
        ts_event=5_000,
        ts_init=5_000,
        cloid=_CLOID,
        symbol="BTC",
        trade_id=trade_id,
        quantity=Decimal("0.5"),
        price=Decimal("41000"),
    )


def _life_through_the_fill(backend: Backend) -> ManualClock:
    """One life that places, fills, and dies with both halves durable.

    The venue is not returned: this life's fill already landed, so the second
    life's subject is the redelivery rather than a barrier heal.
    """
    bus = InMemoryBus()
    clock = ManualClock()
    store = backend.open()
    venue = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
    )
    checks = checkpointer(store, clock=clock)
    checks.recover()  # a life starts the way the runner starts one (ADR-0043 §6)
    manager = ExecutionManager(bus=bus, exchange=venue, checkpointer=checks)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)

    async def scenario() -> None:
        await bus.publish(_crossing_tick(1_000))
        await bus.publish(_redelivered_signal())

    asyncio.run(scenario())

    record = store.get_order(_CLOID)
    assert record is not None and record.state is OrderState.FILLED
    store.close()  # the crash: the process, and its store connection, die
    return clock


def test_a_landed_fill_is_restored_and_a_redelivery_does_not_double_count_it(
    store_backend: Backend,
) -> None:
    """No lost fill and no double-counted one, across a real restart.

    The two halves have different owners. Nothing is lost because ``recover()``
    reads the ledger back before anything else runs — without it the second life
    reports a flat position for a symbol the venue holds 0.5 of, and on paper
    there is no venue to heal that from. Nothing doubles because the *restored*
    saga still carries the trade in its applied set, so the venue's redelivery
    dies at ``Order.record_fill`` and never reaches the ledger at all.

    What this case deliberately does **not** prove is the atomicity itself: the
    crash here lands after both halves are durable, so a split write would pass
    it. The window between two writes is only observable where one of them can
    fail — asserted at the ``ExecutionManager`` seam and in the store contract.
    """
    clock = _life_through_the_fill(store_backend)

    bus = InMemoryBus()
    store = store_backend.open()
    checks = checkpointer(store, clock=clock)
    # The runner's order: the ledger before the order cache (ADR-0043 §6) — one
    # call now, because that ordering is the ``Checkpointer``'s own rule.
    checks.recover()
    manager = ExecutionManager(
        bus=bus,
        exchange=PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
        ),
        checkpointer=checks,
    )
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    portfolio = checks.portfolio.for_strategy("trivial")

    # Nothing lost: the fill the first life booked is there before anything runs.
    restored = portfolio.position("BTC")
    assert restored is not None
    assert restored.size == Decimal("0.5")
    assert restored.entry_price == Decimal("41000")
    assert portfolio.account().cash == GENESIS  # an opening fill realizes nothing
    # And nothing fabricated: a symbol the ledger has no row for stays absent.
    assert portfolio.position("ETH") is None

    # At-least-once redelivery of the venue's own fill, across the restart.
    asyncio.run(bus.publish(_fill_report(f"{_CLOID}-1")))

    unchanged = portfolio.position("BTC")
    assert unchanged is not None
    assert unchanged.size == Decimal("0.5")  # booked once, not twice
    assert portfolio.account().cash == GENESIS
    assert [position.signed_size for position in store.all_positions()] == [Decimal("0.5")]
    store.close()


class _Killed(BaseException):
    """The process died. Raised by the broker the instant a record lands.

    A ``BaseException``, like ``KeyboardInterrupt``, so no containment on the
    way up mistakes it for a handler bug and swallows it.
    """


def _first_tick() -> MarketTick:
    return MarketTick(
        ts_event=1_000,
        ts_init=1_000,
        symbol="BTC",
        price=Decimal("42000"),
        size=Decimal("3"),
        aggressor_side=AggressorSide.SELL,
        trade_id="a",
        seq=1,
    )


def _signals_in_topic(broker: FakeKafkaBroker) -> list[str]:
    return [
        event.signal_id
        for partition in broker.partitions
        for _, value in partition
        if isinstance(event := decode_event(value), Signal)
    ]


def _kafka_life(
    broker: FakeKafkaBroker, backend: Backend, venue: PaperExchange, clock: ManualClock
) -> tuple[KafkaBus, Store]:
    """One process life over Kafka, wired the way the runner wires it.

    The store reopens on the same durable backing. The checkpointer restores
    both read models from it. The host restores the strategy from its snapshot.
    The venue is the remote that outlives every one of our crashes.
    """
    bus = KafkaBus(
        bootstrap_servers="kafka:9092",
        topic="tickwright.events",
        group_id="tickwright",
        producer_factory=broker.producer,
        consumer_factory=broker.consumer,
    )
    store = backend.open()
    checks = checkpointer(store, clock=clock)
    checks.recover()
    host = StrategyHost(bus=bus, clock=clock, store=store, cache=checks.cache)
    host.register(
        SingleShotLimitStrategy(
            strategy_id="trivial",
            bus=bus,
            clock=clock,
            side=Side.BUY,
            quantity=Decimal("0.5"),
            price=Decimal("41000"),
        ),
        symbols={"BTC"},
    )
    host.start()
    manager = ExecutionManager(bus=bus, exchange=venue, checkpointer=checks)
    # The venue reads ticks from each life's bus, so it can price the limit.
    # Its reports go out on its own construction bus, which no life hears.
    bus.subscribe(MarketTick, venue.on_tick)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    return bus, store


async def _run_until_committed(bus: KafkaBus, broker: FakeKafkaBroker) -> None:
    await bus.start()
    await broker.all_committed()
    await bus.close()


def test_on_kafka_a_kill_right_after_the_signal_lands_places_one_order_across_three_lives(
    store_backend: Backend,
) -> None:
    """Issue #350. On Kafka a handler's publish used to reach the topic before
    the handler's own writes. The bus now holds it until the dispatch returns,
    so the snapshot is on disk first. This is the crash that used to double:
    killed the instant the Signal record lands, with the tick still
    uncommitted. Two restarts later, one order."""
    broker = FakeKafkaBroker()
    clock = ManualClock()
    venue = PaperExchange(
        bus=InMemoryBus(),
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
    )

    def kill_on_signal(record: Record) -> None:
        if isinstance(decode_event(record.value), Signal):
            raise _Killed

    # The broker outlives every life, so all three run on its one event loop.
    async def scenario() -> None:
        # Life 1: dies the instant the Signal record lands in the topic.
        broker.on_produce.append(kill_on_signal)
        bus, store = _kafka_life(broker, store_backend, venue, clock)
        await bus.start()
        with pytest.raises(_Killed):
            await bus.publish(_first_tick())
        await bus.close()
        broker.on_produce.clear()
        # What the crash left: the Signal in the topic, its tick uncommitted,
        # and no saga yet. The strategy's snapshot is all that remembers.
        assert _signals_in_topic(broker) == ["trivial:BTC:1"]
        assert broker.committed == [0, 0, 0]
        assert store.get_order(_CLOID) is None
        store.close()

        # Life 2: the tick and the Signal both redeliver. The strategy stays
        # quiet, the manager places once.
        bus, store = _kafka_life(broker, store_backend, venue, clock)
        await _run_until_committed(bus, broker)
        assert _signals_in_topic(broker) == ["trivial:BTC:1"]
        assert store.get_order(_CLOID) is not None
        store.close()

        # Life 3: a restart from stale offsets, so the whole topic redelivers.
        broker.committed = [0, 0, 0]
        bus, store = _kafka_life(broker, store_backend, venue, clock)
        await _run_until_committed(bus, broker)
        assert _signals_in_topic(broker) == ["trivial:BTC:1"]
        store.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))

    first = asyncio.run(venue.fetch_order(OrderRef(cloid=_CLOID, symbol="BTC")))
    second = asyncio.run(
        venue.fetch_order(OrderRef(cloid=derive_cloid("trivial:BTC:2"), symbol="BTC"))
    )
    assert isinstance(first, VenueOrderView) and first.has_record
    assert isinstance(second, VenueOrderView) and not second.has_record
