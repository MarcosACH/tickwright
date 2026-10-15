"""Paper keeps its resting orders across a restart on the same store (#484).

Paper holds its book in memory only. A new process builds a new venue, so the
venue rebuilds its book from the order rows the store kept. Each case builds
paper the way ``app/build.py`` does, through ``paper_store_reads``.
"""

import asyncio
from decimal import Decimal

import pytest
from kafka_fakes import FakeKafkaBroker
from ledgers import GENESIS, checkpointer

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.bus.kafka import KafkaBus
from tickwright.adapters.bus.serde import encode_event
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.app.build import paper_store_reads
from tickwright.domain import (
    AggressorSide,
    EventBus,
    ExecutionReport,
    FillReport,
    MarketTick,
    Order,
    OrderRef,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
    VenueOpenOrder,
    VenueOrderView,
)
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.reconcile import PassResult, ReconcileConfig, Reconciler
from tickwright.observability.testing import capture_events


def _paper(
    store: SQLiteStore, bus: EventBus | None = None, clock: ManualClock | None = None
) -> PaperExchange:
    reads = paper_store_reads(store)
    return PaperExchange(
        bus=bus or InMemoryBus(),
        clock=clock or ManualClock(start_ns=1_000),
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=reads.account_net,
        applied_fills=reads.applied_fills,
        resting_orders=reads.resting_orders,
    )


def _resting(
    cloid: str = "0xa",
    *,
    cum_qty: str = "0",
    state: OrderState = OrderState.LIVE,
    price: Decimal | None = Decimal("41000"),
    time_in_force: TimeInForce | None = TimeInForce.GTC,
    applied_event_ids: tuple[str, ...] = (),
) -> Order:
    """A limit an earlier run left resting, as its order row holds it."""
    return Order.restore(
        cloid=cloid,
        strategy_id="trivial",
        signal_id=f"trivial:BTC:{cloid}",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("2"),
        order_type=OrderType.LIMIT,
        state=state,
        cum_qty=Decimal(cum_qty),
        venue_oid=None,
        reason=None,
        cancel_requested=False,
        cancel_requested_ts=None,
        cancel_signal_id=None,
        applied_event_ids=applied_event_ids,
        price=price,
        time_in_force=time_in_force,
    )


def test_a_restarted_paper_venue_lists_the_orders_an_earlier_run_left_resting() -> None:
    store = SQLiteStore(":memory:")
    store.checkpoint(_resting("0xa"), ts_ns=1_000)
    exchange = _paper(store)

    async def scenario() -> object:
        await exchange.start()
        return await exchange.fetch_open_orders()

    assert asyncio.run(scenario()) == [VenueOpenOrder(symbol="BTC", cloid="0xa")]


def test_a_rebuilt_order_fills_only_the_size_an_earlier_run_left_open() -> None:
    # It asked for 2 and an earlier run filled 0.5. Filling 2 again would
    # over-fill the saga, so the venue must work only the 1.5 still open.
    store = SQLiteStore(":memory:")
    store.checkpoint(_resting("0xa", cum_qty="0.5"), ts_ns=1_000)
    bus = InMemoryBus()
    exchange = _paper(store, bus)
    fills: list[FillReport] = []

    async def record(report: FillReport) -> None:
        fills.append(report)

    bus.subscribe(FillReport, record)

    async def scenario() -> None:
        await exchange.start()
        # A tick below the BUY limit at 41000 crosses it.
        await bus.publish(
            MarketTick(
                ts_event=2_000,
                ts_init=2_000,
                symbol="BTC",
                price=Decimal("40000"),
                size=Decimal("5"),
                aggressor_side=AggressorSide.SELL,
                trade_id="t2000",
                seq=0,
            )
        )

    asyncio.run(scenario())

    assert [(fill.cloid, fill.quantity, fill.price) for fill in fills] == [
        ("0xa", Decimal("1.5"), Decimal("41000"))
    ]


def test_a_restarted_paper_venue_rebuilds_only_the_orders_the_venue_confirmed_open() -> None:
    # Only LIVE and PARTIALLY_FILLED rows are orders the venue confirmed it
    # holds. A PENDING or SUBMITTED row may never have reached paper, so
    # rebuilding it would invent an order nobody placed. Reconcile resolves
    # those FAILED. Finished rows, rows with no price and IOC rows never rest.
    store = SQLiteStore(":memory:")
    store.checkpoint(_resting("0xlive"), ts_ns=1_000)
    store.checkpoint(
        _resting("0xpartial", state=OrderState.PARTIALLY_FILLED, cum_qty="0.5"), ts_ns=1_000
    )
    store.checkpoint(_resting("0xpending", state=OrderState.PENDING), ts_ns=1_000)
    store.checkpoint(_resting("0xsubmitted", state=OrderState.SUBMITTED), ts_ns=1_000)
    store.checkpoint(_resting("0xfilled", state=OrderState.FILLED, cum_qty="2"), ts_ns=1_000)
    store.checkpoint(_resting("0xcancelled", state=OrderState.CANCELLED), ts_ns=1_000)
    store.checkpoint(_resting("0xnoprice", price=None, time_in_force=None), ts_ns=1_000)
    store.checkpoint(
        _resting("0xioc", state=OrderState.PARTIALLY_FILLED, time_in_force=TimeInForce.IOC),
        ts_ns=1_000,
    )
    exchange = _paper(store)

    async def scenario() -> object:
        await exchange.start()
        return await exchange.fetch_open_orders()

    orders = asyncio.run(scenario())

    assert isinstance(orders, list)
    assert set(orders) == {
        VenueOpenOrder(symbol="BTC", cloid="0xlive"),
        VenueOpenOrder(symbol="BTC", cloid="0xpartial"),
    }


def _second_life(store: SQLiteStore, clock: ManualClock) -> tuple[InMemoryBus, Reconciler]:
    """A new process on the same store: rebuilt cache, manager, paper and reconciler."""
    bus = InMemoryBus()
    checks = checkpointer(store, clock=clock)
    checks.cache.rebuild()
    exchange = _paper(store, bus, clock)
    manager = ExecutionManager(bus=bus, exchange=exchange, checkpointer=checks)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    reconciler = Reconciler(
        bus=bus, clock=clock, exchange=exchange, cache=checks.cache, config=ReconcileConfig()
    )
    asyncio.run(exchange.start())
    return bus, reconciler


# A LIVE row applied its LIVE ack. A row that part-filled on arrival went from
# SUBMITTED straight to PARTIALLY_FILLED, so it never applied one.
_WORKING_ROWS = pytest.mark.parametrize(
    ("state", "cum_qty", "applied"),
    [
        (OrderState.LIVE, "0", ("0xa:submitted", "0xa:live")),
        (OrderState.PARTIALLY_FILLED, "0.5", ("0xa:submitted", "0xa:fill:0xa-1")),
    ],
    ids=["live", "partially-filled"],
)


@_WORKING_ROWS
def test_reconcile_finds_a_rebuilt_order_and_never_ghosts_it(
    state: OrderState, cum_qty: str, applied: tuple[str, ...]
) -> None:
    # Paper still holds the order, so reconcile must find it. A ghost verdict
    # would end the saga while paper can still fill it.
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    store.checkpoint(
        _resting("0xa", state=state, cum_qty=cum_qty, applied_event_ids=applied), ts_ns=1_000
    )
    _, reconciler = _second_life(store, clock)

    async def scenario() -> None:
        assert await reconciler.reconcile_startup() is True
        await clock.sleep(ReconcileConfig().ghost_grace_seconds)
        assert await reconciler.reconcile_open_orders() is PassResult.PROVED

    with capture_events() as logs:
        asyncio.run(scenario())

    assert "ghost.reconciled" not in [log["event"] for log in logs]
    recovered = store.get_order("0xa")
    assert recovered is not None
    assert (recovered.state, recovered.cum_qty) == (state, Decimal(cum_qty))


def test_a_rebuilt_order_books_its_next_fill_on_the_saga() -> None:
    # An earlier run booked fill 0xa-1 for 0.5. The next fill must carry a new
    # trade id, or the saga drops it as a duplicate and misses 1.5.
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    store.checkpoint(
        _resting(
            "0xa",
            state=OrderState.PARTIALLY_FILLED,
            cum_qty="0.5",
            applied_event_ids=("0xa:submitted", "0xa:fill:0xa-1"),
        ),
        ts_ns=1_000,
    )
    bus, _ = _second_life(store, clock)

    # A tick below the BUY limit at 41000 crosses it.
    asyncio.run(
        bus.publish(
            MarketTick(
                ts_event=3_000,
                ts_init=3_000,
                symbol="BTC",
                price=Decimal("40000"),
                size=Decimal("5"),
                aggressor_side=AggressorSide.SELL,
                trade_id="t3000",
                seq=0,
            )
        )
    )

    recovered = store.get_order("0xa")
    assert recovered is not None
    assert (recovered.state, recovered.cum_qty) == (OrderState.FILLED, Decimal("2"))


def _crossing_tick(ts_ns: int) -> MarketTick:
    """A tick below the BUY limit at 41000, so it crosses it."""
    return MarketTick(
        ts_event=ts_ns,
        ts_init=ts_ns,
        symbol="BTC",
        price=Decimal("40000"),
        size=Decimal("5"),
        aggressor_side=AggressorSide.SELL,
        trade_id=f"t{ts_ns}",
        seq=0,
    )


def _earlier_fill(trade_id: str, quantity: str) -> FillReport:
    """A fill paper published for 0xa in an earlier run."""
    return FillReport(
        ts_event=1_500,
        ts_init=1_500,
        cloid="0xa",
        symbol="BTC",
        trade_id=trade_id,
        quantity=Decimal(quantity),
        price=Decimal("41000"),
        fee=Decimal("0"),
    )


def _kafka_second_life(
    store: SQLiteStore, clock: ManualClock, broker: FakeKafkaBroker
) -> tuple[KafkaBus, PaperExchange]:
    """A new process on the same store and the same Kafka topic."""
    bus = KafkaBus(
        bootstrap_servers="kafka:9092",
        topic="tickwright.events",
        group_id="tickwright",
        producer_factory=broker.producer,
        consumer_factory=broker.consumer,
    )
    checks = checkpointer(store, clock=clock)
    checks.cache.rebuild()
    exchange = _paper(store, bus, clock)
    manager = ExecutionManager(bus=bus, exchange=exchange, checkpointer=checks)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    return bus, exchange


def _restart_on_kafka(store: SQLiteStore, broker: FakeKafkaBroker) -> PaperExchange:
    """Start a second life, let Kafka redeliver, then cross the rebuilt order."""
    bus, exchange = _kafka_second_life(store, ManualClock(start_ns=2_000), broker)

    async def scenario() -> None:
        # The runner's order. Paper reads the store before any redelivery lands.
        await bus.start()
        await exchange.start()
        await broker.all_committed()
        await bus.publish(_crossing_tick(3_000))
        await bus.close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=5))
    return exchange


def test_on_kafka_a_redelivered_fill_books_on_the_rebuilt_order() -> None:
    # An earlier run published fill 0xa-1 for 1, then crashed before Kafka
    # committed it. The saga never booked it, so paper rebuilds the order at 2.
    # Kafka redelivers the fill after that. If paper ignores it, paper works 2
    # more, names that fill 0xa-1 again, and the saga drops it as a duplicate.
    store = SQLiteStore(":memory:")
    store.checkpoint(_resting("0xa", applied_event_ids=("0xa:submitted", "0xa:live")), ts_ns=1_000)
    broker = FakeKafkaBroker()
    redelivered = _earlier_fill("0xa-1", "1")
    broker.produce(redelivered.partition_key.encode(), encode_event(redelivered))

    exchange = _restart_on_kafka(store, broker)

    recovered = store.get_order("0xa")
    assert recovered is not None
    assert (recovered.state, recovered.cum_qty) == (OrderState.FILLED, Decimal("2"))
    view = asyncio.run(exchange.fetch_order(OrderRef(cloid="0xa", symbol="BTC")))
    assert isinstance(view, VenueOrderView)
    assert [(fill.trade_id, fill.quantity) for fill in view.fills] == [
        ("0xa-1", Decimal("1")),
        ("0xa-2", Decimal("1")),
    ]
