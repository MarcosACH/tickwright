"""Paper keeps its resting orders across a restart on the same store (#484).

Paper holds its book in memory only. A new process builds a new venue, so the
venue rebuilds its book from the order rows the store kept. Each case builds
paper the way ``app/build.py`` does, through ``paper_store_reads``.
"""

import asyncio
from decimal import Decimal

from ledgers import GENESIS

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.app.build import paper_store_reads
from tickwright.domain import (
    AggressorSide,
    FillReport,
    MarketTick,
    Order,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
    VenueOpenOrder,
)


def _paper(store: SQLiteStore, bus: InMemoryBus | None = None) -> PaperExchange:
    reads = paper_store_reads(store)
    return PaperExchange(
        bus=bus or InMemoryBus(),
        clock=ManualClock(start_ns=1_000),
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
        applied_event_ids=(),
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
