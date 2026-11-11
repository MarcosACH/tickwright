"""An IOC order the venue fills only in part, end to end (issue #629): the real
``ExecutionManager`` and ``Reconciler`` against the real ``HyperliquidExchange``,
with only the HTTP transport fake.

The venue drops the part an IOC order could not fill and answers `filled`. A
`filled` report moves no saga, so without an explicit end the saga stays
PARTIALLY_FILLED forever and the strategy waits on it.
"""

import asyncio
from dataclasses import replace
from decimal import Decimal

from exchange_contract import OrderEnd, assert_order_ends
from hyperliquid_fakes import (
    TEST_SIGNING_KEY,
    FakeExchangeApi,
    fill_entry,
    filled_response,
    order_status_response,
    resting_response,
)
from ledgers import checkpointer
from pydantic import SecretStr

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    ExecutionReport,
    InstrumentSpec,
    MarketTick,
    MarkTick,
    Order,
    OrderState,
    OrderType,
    PlaceSignal,
    Side,
    TimeInForce,
    derive_cloid,
)
from tickwright.domain.enums import AggressorSide
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.reconcile import PassResult, ReconcileConfig, Reconciler
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

ACK_MS = 1_700_000_060_000
FILL_MS = ACK_MS + 5
NS = ACK_MS * 1_000_000

# The soak's cycle 1604: a market buy of 0.00015 BTC, of which 0.00008 filled.
MARKET_BUY = PlaceSignal(
    ts_event=NS,
    ts_init=NS,
    strategy_id="live",
    symbol="BTC",
    seq=1,
    side=Side.BUY,
    quantity=Decimal("0.00015"),
    order_type=OrderType.MARKET,
    time_in_force=TimeInForce.IOC,
)
PART_FILL = fill_entry(oid=91, tid=556, px="104000.0", sz="0.00008", time=FILL_MS)


def _run(signal: PlaceSignal, post: FakeExchangeApi, *, passes: int = 0) -> Order:
    async def main() -> Order:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=NS)
        config = ReconcileConfig()
        exchange = HyperliquidExchange(
            config=HyperliquidConfig(
                testnet=True, symbols=["BTC"], signing_key=SecretStr(TEST_SIGNING_KEY)
            ),
            bus=bus,
            clock=clock,
            universe=HyperliquidUniverse(specs={"BTC": BTC_SPEC}, asset_indices={"BTC": 3}),
            post=post,
            startup_timeout_seconds=60.0,
            cut_part_wait_seconds=config.open_order_interval_seconds,
        )
        # A market order is priced from the latest trade and mark (ADR-0059).
        await exchange.on_tick(
            MarketTick(
                symbol="BTC",
                price=Decimal("104000"),
                size=Decimal("1"),
                aggressor_side=AggressorSide.BUY,
                trade_id="1",
                seq=0,
                venue_trade_id=True,
                ts_event=NS,
                ts_init=NS,
            )
        )
        await exchange.on_mark(
            MarkTick(symbol="BTC", price=Decimal("104000"), ts_event=NS, ts_init=NS)
        )
        store = SQLiteStore(":memory:")
        checks = checkpointer(store, clock=clock)
        manager = ExecutionManager(bus=bus, exchange=exchange, checkpointer=checks)
        bus.subscribe(ExecutionReport, manager.on_execution_report)
        await manager.on_signal(signal)
        reconciler = Reconciler(
            bus=bus, clock=clock, exchange=exchange, cache=checks.cache, config=config
        )
        for _ in range(passes):
            clock.advance_to(
                clock.timestamp_ns() + int(config.open_order_interval_seconds * 1_000_000_000)
            )
            assert await reconciler.reconcile_open_orders() is PassResult.PROVED
        order = store.get_order(derive_cloid(signal.signal_id))
        assert order is not None
        return order

    return asyncio.run(main())


def test_a_market_order_filled_in_part_ends_cancelled_at_placement() -> None:
    post = FakeExchangeApi(
        {
            "order": filled_response(oid=91, total_sz="0.00008", avg_px="104000.0"),
            "userFills": [PART_FILL],
        }
    )

    order = _run(MARKET_BUY, post)

    assert_order_ends(
        order,
        case=OrderEnd.IOC_PARTIAL_FILL,
        filled=Decimal("0.00008"),
        name="HyperliquidExchange",
    )


def test_reconcile_ends_a_market_order_filled_in_part_once_two_passes_read_the_same_fills() -> None:
    # The fills read right after placement dies, so placement cannot end the
    # order. A restart lands here too. Reconcile reads the venue's `filled`
    # record and the fills. It ends the cut part once the fill total has held
    # for one open-order interval (ADR-0057, #478).

    post = FakeExchangeApi(
        {
            "order": filled_response(oid=91, total_sz="0.00008", avg_px="104000.0"),
            "userFills": ConnectionError("connection refused"),
            "orderStatus": order_status_response(
                cloid=derive_cloid(MARKET_BUY.signal_id),
                status="filled",
                oid=91,
                placed_ms=ACK_MS,
                tif="Ioc",
            ),
            "userFillsByTime": [PART_FILL],
        }
    )

    order = _run(MARKET_BUY, post, passes=2)

    assert order.state is OrderState.CANCELLED
    assert order.cum_qty == Decimal("0.00008")


def test_a_gtc_order_the_venue_calls_filled_stays_open_while_its_fills_read_short() -> None:
    # A GTC order the venue calls `filled` filled in full. If our fills read is
    # short, a fill is missing, not dropped. Ending the saga would lose that
    # fill for good, because reconcile stops watching a terminal saga. So only
    # reduce-only and IOC orders may end short.
    gtc_buy = replace(
        MARKET_BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("104000"),
    )
    post = FakeExchangeApi(
        {
            "order": resting_response(oid=91),
            "orderStatus": order_status_response(
                cloid=derive_cloid(gtc_buy.signal_id),
                status="filled",
                oid=91,
                placed_ms=ACK_MS,
                tif="Gtc",
            ),
            "userFillsByTime": [PART_FILL],
        }
    )

    order = _run(gtc_buy, post, passes=3)

    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.cum_qty == Decimal("0.00008")
