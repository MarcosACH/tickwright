"""An acked order the venue no longer has a record of, end to end (issue #242):
the real ``Reconciler`` and ``ExecutionManager`` against the real
``HyperliquidExchange``, with only the HTTP transport fake.

Hyperliquid drops order records by count and keeps fills for years. Before
this slice the adapter answered ``unknownOid`` with an empty view, so the
reconciler read a filled order as one that never landed and rejected it. Now
the fill history is read by the ack's oid, and the fill heals the saga.
"""

import asyncio
from decimal import Decimal

from hyperliquid_fakes import (
    TEST_SIGNING_KEY,
    FakeExchangeApi,
    fill_entry,
    filled_response,
    request_type,
)
from ledgers import checkpointer
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    ExecutionReport,
    InstrumentSpec,
    Order,
    OrderEvent,
    OrderFilled,
    OrderLive,
    OrderPlaced,
    OrderRejected,
    OrderState,
    OrderSubmitted,
    OrderType,
    PlaceSignal,
    Side,
    TimeInForce,
    derive_cloid,
)
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.reconcile import ReconcileConfig, Reconciler
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidUniverse,
)

BTC_SPEC = InstrumentSpec(
    symbol="BTC",
    sz_decimals=5,
    max_decimals=6,
    min_notional=Decimal("10"),
    max_sig_figs=5,
)

CLOID = "0x" + "ab" * 16
ACK_MS = 1_700_000_060_000


def _acked_saga() -> Order:
    """A resting order whose ack carried the venue's oid and a time."""
    order = Order(
        cloid=CLOID,
        strategy_id="live",
        signal_id="live:BTC:1",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.LIMIT,
    )
    order.state = OrderState.SUBMITTED
    order.apply(
        OrderLive(
            ts_event=ACK_MS * 1_000_000,
            ts_init=ACK_MS * 1_000_000,
            cloid=CLOID,
            strategy_id="live",
            signal_id="live:BTC:1",
            symbol="BTC",
            venue_oid="91",
        )
    )
    return order


FILL_MS = ACK_MS + 5_000
"""When the venue filled the order: after the ack, inside the windowed read."""


def _make_exchange(
    post: FakeExchangeApi, bus: InMemoryBus, clock: ManualClock
) -> HyperliquidExchange:
    return HyperliquidExchange(
        config=HyperliquidConfig(
            testnet=True, symbols=["BTC"], signing_key=SecretStr(TEST_SIGNING_KEY)
        ),
        bus=bus,
        clock=clock,
        universe=HyperliquidUniverse(specs={"BTC": BTC_SPEC}, asset_indices={"BTC": 3}),
        post=post,
        startup_timeout_seconds=60.0,
    )


def test_a_fill_behind_a_dropped_record_heals_the_saga_instead_of_rejecting_it() -> None:
    async def main() -> tuple[SQLiteStore, list[OrderEvent], FakeExchangeApi]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=(ACK_MS + 60_000) * 1_000_000)
        post = FakeExchangeApi(
            {
                "orderStatus": {"status": "unknownOid"},
                "userFillsByTime": [
                    fill_entry(oid=90, tid=555, px="43250.0", sz="1.0", time=FILL_MS),
                    fill_entry(oid=91, tid=556, px="43250.0", sz="0.5", time=FILL_MS),
                ],
            }
        )
        exchange = _make_exchange(post, bus, clock)
        store = SQLiteStore(":memory:")
        store.checkpoint(_acked_saga(), ts_ns=500)
        checks = checkpointer(store, clock=clock)
        checks.cache.rebuild()
        manager = ExecutionManager(bus=bus, exchange=exchange, checkpointer=checks)
        bus.subscribe(ExecutionReport, manager.on_execution_report)
        events: list[OrderEvent] = []

        async def collect(event: OrderEvent) -> None:
            events.append(event)

        bus.subscribe(OrderEvent, collect)
        reconciler = Reconciler(
            bus=bus, clock=clock, exchange=exchange, cache=checks.cache, config=ReconcileConfig()
        )
        assert await reconciler.reconcile_startup() is True
        return store, events, post

    store, events, post = asyncio.run(main())

    # The record was gone, so the venue was asked for the fill history by the
    # ack's oid, and from the ack time less the skew allowance.
    assert [q["type"] for _, q in post.requests] == ["orderStatus", "userFillsByTime"]
    assert post.requests[1][1]["startTime"] == ACK_MS - 60_000

    # The fill healed the saga. Nothing rejected it.
    recovered = store.get_order(CLOID)
    assert recovered is not None
    assert recovered.state is OrderState.FILLED
    assert recovered.cum_qty == Decimal("0.5")
    assert [type(e) for e in events] == [OrderFilled]
    assert not any(isinstance(e, OrderRejected) for e in events)


def test_an_order_that_filled_on_placement_is_healed_by_its_oid_once_the_record_is_gone() -> None:
    # Issue #328. The order fills on placement, but the fills read right after
    # dies in transport. By the time reconcile looks, the venue has dropped the
    # order record (about 2000 later orders). The saga must still hold the oid
    # the placement gave it, so the fill history is read by that oid and the
    # fill lands. Before the fix the saga sat SUBMITTED with no oid, reconcile
    # read `unknownOid` as "never landed", and the fill was lost.
    signal = PlaceSignal(
        ts_event=ACK_MS * 1_000_000,
        ts_init=ACK_MS * 1_000_000,
        strategy_id="live",
        symbol="BTC",
        seq=1,
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        price=Decimal("43250"),
    )
    cloid = derive_cloid(signal.signal_id)

    async def main() -> tuple[SQLiteStore, list[OrderEvent], FakeExchangeApi]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=ACK_MS * 1_000_000)
        post = FakeExchangeApi(
            {
                "order": filled_response(oid=91, total_sz="0.5", avg_px="43250.0"),
                # The read right after placement is the whole recent history.
                # It dies. The read reconcile makes is windowed from the ack
                # time, and by then the record is gone.
                "userFills": ConnectionError("connection refused"),
                "orderStatus": {"status": "unknownOid"},
                "userFillsByTime": [
                    fill_entry(oid=91, tid=556, px="43250.0", sz="0.5", time=FILL_MS)
                ],
            }
        )
        exchange = _make_exchange(post, bus, clock)
        store = SQLiteStore(":memory:")
        checks = checkpointer(store, clock=clock)
        manager = ExecutionManager(bus=bus, exchange=exchange, checkpointer=checks)
        bus.subscribe(ExecutionReport, manager.on_execution_report)
        events: list[OrderEvent] = []

        async def collect(event: OrderEvent) -> None:
            events.append(event)

        bus.subscribe(OrderEvent, collect)
        await manager.on_signal(signal)

        placed = store.get_order(cloid)
        assert placed is not None
        assert (placed.state, placed.venue_oid) == (OrderState.LIVE, "91")
        assert placed.acked_ts_ns == ACK_MS * 1_000_000

        reconciler = Reconciler(
            bus=bus, clock=clock, exchange=exchange, cache=checks.cache, config=ReconcileConfig()
        )
        assert await reconciler.reconcile_open_orders() is True
        return store, events, post

    store, events, post = asyncio.run(main())

    # Reconcile read the fill history by the placement's oid, from the ack time
    # less the skew allowance, and found the fill.
    assert [request_type(url, q) for url, q in post.requests] == [
        "order",
        "userFills",
        "orderStatus",
        "userFillsByTime",
    ]
    assert post.requests[3][1]["startTime"] == ACK_MS - 60_000

    healed = store.get_order(cloid)
    assert healed is not None
    assert healed.state is OrderState.FILLED
    assert healed.cum_qty == Decimal("0.5")
    assert [type(e) for e in events] == [OrderPlaced, OrderSubmitted, OrderLive, OrderFilled]
