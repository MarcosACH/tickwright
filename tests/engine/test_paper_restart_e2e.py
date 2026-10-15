"""Paper keeps its resting orders across a restart on the same store (#484).

Paper holds its book in memory only. A new process builds a new venue, so the
venue rebuilds its book from the order rows the store kept. Each case builds
paper the way ``app/build.py`` does, through ``paper_store_reads``.
"""

import asyncio
from decimal import Decimal

import pytest
from ledgers import GENESIS, checkpointer

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.app.build import paper_store_reads
from tickwright.domain import (
    AggressorSide,
    ExecutionReport,
    FillReport,
    MarketTick,
    Order,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
    VenueOpenOrder,
)
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.reconcile import PassResult, ReconcileConfig, Reconciler
from tickwright.observability.testing import capture_events


def _paper(
    store: SQLiteStore, bus: InMemoryBus | None = None, clock: ManualClock | None = None
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
