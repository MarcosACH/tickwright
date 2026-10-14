"""A shrunk reduce-only order whose fills read failed, end to end (issue #478):
the real ``ExecutionManager`` and ``Reconciler`` against the real
``HyperliquidExchange``, with only the HTTP transport fake.

The venue shrinks a reduce-only order to the position and answers `filled`
(ADR-0057, probe P1). When the fills read right after placement fails, the
adapter leaves the order to reconcile. Reconcile heals the fills, but a
`filled` record moves no saga, so the saga must still end CANCELLED for the
cut part.
"""

import asyncio
from decimal import Decimal

from hyperliquid_fakes import (
    TEST_SIGNING_KEY,
    FakeExchangeApi,
    fill_entry,
    filled_response,
    order_status_response,
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
    OrderState,
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

ACK_MS = 1_700_000_060_000
FILL_MS = ACK_MS + 5

# Short 0.3. The strategy sends a reduce-only IOC buy of 0.5 to close it.
SIGNAL = PlaceSignal(
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
    reduce_only=True,
)
CLOID = derive_cloid(SIGNAL.signal_id)


def _shrunk_venue(*fills: dict) -> FakeExchangeApi:
    """The venue after it shrank the 0.5 buy to the 0.3 short and filled it.
    The fills read right after placement dies. Reconcile's reads succeed."""
    return FakeExchangeApi(
        {
            "order": filled_response(oid=91, total_sz="0.3", avg_px="43250.0"),
            "userFills": ConnectionError("connection refused"),
            "orderStatus": order_status_response(
                cloid=CLOID, status="filled", oid=91, placed_ms=ACK_MS, reduce_only=True
            ),
            "userFillsByTime": list(fills),
        }
    )


def _place_then_reconcile(post: FakeExchangeApi, *, passes: int) -> tuple[Order, list[OrderEvent]]:
    async def main() -> tuple[Order, list[OrderEvent]]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=ACK_MS * 1_000_000)
        exchange = HyperliquidExchange(
            config=HyperliquidConfig(
                testnet=True, symbols=["BTC"], signing_key=SecretStr(TEST_SIGNING_KEY)
            ),
            bus=bus,
            clock=clock,
            universe=HyperliquidUniverse(specs={"BTC": BTC_SPEC}, asset_indices={"BTC": 3}),
            post=post,
            startup_timeout_seconds=60.0,
        )
        store = SQLiteStore(":memory:")
        checks = checkpointer(store, clock=clock)
        manager = ExecutionManager(bus=bus, exchange=exchange, checkpointer=checks)
        bus.subscribe(ExecutionReport, manager.on_execution_report)
        events: list[OrderEvent] = []

        async def collect(event: OrderEvent) -> None:
            events.append(event)

        bus.subscribe(OrderEvent, collect)
        await manager.on_signal(SIGNAL)
        reconciler = Reconciler(
            bus=bus, clock=clock, exchange=exchange, cache=checks.cache, config=ReconcileConfig()
        )
        for _ in range(passes):
            assert await reconciler.reconcile_open_orders() is True
        order = store.get_order(CLOID)
        assert order is not None
        return order, events

    return asyncio.run(main())


def test_a_shrunk_order_ends_cancelled_once_two_passes_read_the_same_fills() -> None:
    post = _shrunk_venue(fill_entry(oid=91, tid=556, px="43250.0", sz="0.3", time=FILL_MS))

    order, _events = _place_then_reconcile(post, passes=2)

    assert order.state is OrderState.CANCELLED
    assert order.cum_qty == Decimal("0.3")
