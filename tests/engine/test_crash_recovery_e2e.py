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
from ledgers import GENESIS, checkpointer
from store_backends import (
    STORE_BACKEND_PARAMS,
    PostgresBackend,
    SQLiteBackend,
    resolve_backend,
)
from venue_doubles import VenueLink

from tickwright.adapters.bus import InMemoryBus
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
    derive_cloid,
)
from tickwright.engine.barrier import StartupBarrier
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.reconcile import ReconcileConfig, Reconciler
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

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
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
        asyncio.run(feed.start())

    # The write-ahead intent is the durable truth the crash left behind.
    record = store.get_order(_CLOID)
    assert record is not None and record.state is OrderState.PENDING
    store.close()  # the crash: the process, and its store connection, die
    return venue, clock


def _second_life(
    backend: Backend, venue: PaperExchange, clock: ManualClock
) -> tuple[InMemoryBus, Reconciler, list[OrderEvent], Store]:
    """Recovery wiring over the survivors: a store reopened on the same durable
    backing, a Cache rebuilt from it, a fresh bus and manager."""
    bus = InMemoryBus()
    store = backend.open()
    checks = checkpointer(store, clock=clock)
    cache = checks.cache
    cache.rebuild()
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
    view = asyncio.run(venue.fetch_order(_CLOID))
    assert view is not None
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
    view = asyncio.run(venue.fetch_order(_CLOID))
    assert view is not None and not view.has_record
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
