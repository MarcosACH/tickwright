"""A shrunk reduce-only order whose fills read failed, end to end (issue #478):
the real ``ExecutionManager`` and ``Reconciler`` against the real
``HyperliquidExchange``, with only the HTTP transport fake.

The venue shrinks a reduce-only order to the position and answers `filled`
(ADR-0057, probe P1). When the fills read right after placement fails, the
adapter leaves the order to reconcile. Reconcile heals the fills, but a
`filled` record moves no saga, so the saga must still end CANCELLED for the
cut part.

A resting GTC order takes the same path (issue #479). The venue holds it at
the position size, and its fills reach the engine only through reconcile.
"""

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest
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
    Order,
    OrderCancelled,
    OrderEvent,
    OrderState,
    OrderType,
    PlaceSignal,
    Side,
    TimeInForce,
    derive_cloid,
)
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


def _shrunk_venue(
    *fills: dict,
    placement_fills: object = None,
    reconcile_fills: object = None,
    total_sz: str = "0.3",
) -> FakeExchangeApi:
    """The venue after it shrank the 0.5 buy to the 0.3 short and filled it.
    By default the fills read right after placement dies, and reconcile reads
    ``fills``."""
    return FakeExchangeApi(
        {
            "order": filled_response(oid=91, total_sz=total_sz, avg_px="43250.0"),
            "userFills": placement_fills or ConnectionError("connection refused"),
            "orderStatus": order_status_response(
                cloid=CLOID, status="filled", oid=91, placed_ms=ACK_MS, reduce_only=True
            ),
            "userFillsByTime": reconcile_fills or list(fills),
        }
    )


def _place_then_reconcile(
    post: FakeExchangeApi, *, passes: int, signal: PlaceSignal = SIGNAL
) -> tuple[Order, list[OrderEvent]]:
    async def main() -> tuple[Order, list[OrderEvent]]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=ACK_MS * 1_000_000)
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
            # What the composition root hands it: the engine's open-order interval.
            shrink_wait_seconds=config.open_order_interval_seconds,
        )
        store = SQLiteStore(":memory:")
        checks = checkpointer(store, clock=clock)
        manager = ExecutionManager(bus=bus, exchange=exchange, checkpointer=checks)
        bus.subscribe(ExecutionReport, manager.on_execution_report)
        events: list[OrderEvent] = []

        async def collect(event: OrderEvent) -> None:
            events.append(event)

        bus.subscribe(OrderEvent, collect)
        await manager.on_signal(signal)
        reconciler = Reconciler(
            bus=bus, clock=clock, exchange=exchange, cache=checks.cache, config=config
        )
        for _ in range(passes):
            # Paced like the open-order cadence: one interval, then a pass.
            clock.advance_to(
                clock.timestamp_ns() + int(config.open_order_interval_seconds * 1_000_000_000)
            )
            assert await reconciler.reconcile_open_orders() is PassResult.PROVED
        order = store.get_order(CLOID)
        assert order is not None
        return order, events

    return asyncio.run(main())


def test_a_shrunk_order_ends_cancelled_once_two_passes_read_the_same_fills() -> None:
    post = _shrunk_venue(fill_entry(oid=91, tid=556, px="43250.0", sz="0.3", time=FILL_MS))

    order, _events = _place_then_reconcile(post, passes=2)

    assert order.state is OrderState.CANCELLED
    assert order.cum_qty == Decimal("0.3")


def test_one_pass_alone_leaves_a_shrunk_order_open() -> None:
    # The fills the venue holds may not all be readable yet. A fill that lands
    # after CANCELLED is an illegal move and would be lost. So one read is not
    # proof that the fills are complete. The saga waits for a second pass.
    post = _shrunk_venue(fill_entry(oid=91, tid=556, px="43250.0", sz="0.3", time=FILL_MS))

    order, _events = _place_then_reconcile(post, passes=1)

    assert order.state is OrderState.PARTIALLY_FILLED
    assert order.cum_qty == Decimal("0.3")


@pytest.mark.parametrize(("passes", "state"), [(2, "open"), (3, "cancelled")])
def test_a_fill_listed_late_starts_the_wait_again(passes: int, state: str) -> None:
    # The venue filled 0.2 and 0.1, but lists the 0.1 late. The read right
    # after placement holds only the 0.2, a short read (#480). So does the
    # first reconcile pass. The second pass sees 0.3, a new total, so it is
    # not yet proof. The third agrees with the second and ends the saga.
    first = fill_entry(oid=91, tid=556, px="43250.0", sz="0.2", time=FILL_MS)
    late = fill_entry(oid=91, tid=557, px="43250.0", sz="0.1", time=FILL_MS)
    reads = 0

    def listed_so_far(_payload: dict) -> list[dict]:
        nonlocal reads
        reads += 1
        return [first] if reads == 1 else [first, late]

    post = _shrunk_venue(placement_fills=[first], reconcile_fills=listed_so_far)

    order, _events = _place_then_reconcile(post, passes=passes)

    assert order.cum_qty == Decimal("0.3")
    if state == "open":
        assert order.state is OrderState.PARTIALLY_FILLED
    else:
        assert order.state is OrderState.CANCELLED


def test_a_reduce_only_order_filled_to_its_full_size_ends_filled() -> None:
    # Against a short of 0.5 or more, the venue does not shrink the buy. Its
    # fills reach the asked size, so there is no cut part to cancel.
    post = _shrunk_venue(
        fill_entry(oid=91, tid=556, px="43250.0", sz="0.5", time=FILL_MS), total_sz="0.5"
    )

    order, events = _place_then_reconcile(post, passes=2)

    assert order.state is OrderState.FILLED
    assert order.cum_qty == Decimal("0.5")
    assert not any(isinstance(event, OrderCancelled) for event in events)


def test_a_resting_order_the_venue_shrank_ends_cancelled_once_it_fills() -> None:
    # Short 0.3. A reduce-only GTC buy of 0.5 rests, and the venue holds it at
    # 0.3. The first pass finds it still resting. Then it fills for 0.3, and
    # the next two passes read the same fills.
    resting_signal = replace(SIGNAL, time_in_force=TimeInForce.GTC)
    fill = fill_entry(oid=91, tid=556, px="43250.0", sz="0.3", time=FILL_MS)
    reads = 0

    def order_status(_payload: dict) -> dict:
        nonlocal reads
        reads += 1
        return order_status_response(
            cloid=CLOID,
            status="open" if reads == 1 else "filled",
            oid=91,
            placed_ms=ACK_MS,
            reduce_only=True,
        )

    def fills(_payload: dict) -> list[dict]:
        return [] if reads == 1 else [fill]

    post = FakeExchangeApi(
        {
            "order": resting_response(oid=91),
            "orderStatus": order_status,
            "userFillsByTime": fills,
        }
    )

    order, _events = _place_then_reconcile(post, passes=3, signal=resting_signal)

    assert order.state is OrderState.CANCELLED
    assert order.cum_qty == Decimal("0.3")
