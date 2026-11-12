"""A venue order that rests under a terminal saga, end to end (issue #642).

The real ``Reconciler`` and ``ExecutionManager`` against the real
``HyperliquidExchange``, with only the HTTP transport fake.

A client timeout does not prove the venue never got the place request. So the
venue can accept an order after the in-flight budget resolved its saga
``FAILED``. Before this slice nothing looked at that order again. Now the
open-order cycle finds it and cancels it, and the saga stays terminal
(ADR-0063).
"""

import asyncio
from decimal import Decimal

import pytest
from hyperliquid_fakes import (
    TEST_SIGNING_KEY,
    FakeExchangeApi,
    answers_in_turn,
    order_status_response,
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
    OrderState,
    OrderType,
    PlaceSignal,
    Side,
    TimeInForce,
    derive_cloid,
)
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.reconcile import PassResult, ReconcileConfig, Reconciler
from tickwright.observability.testing import capture_events
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

SENT_MS = 1_700_000_060_000
LANDED_MS = SENT_MS + 15_300
"""When the venue accepted the order: after the in-flight budget gave up (#640)."""

STRAY_OID = 77


def _signal() -> PlaceSignal:
    return PlaceSignal(
        ts_event=SENT_MS * 1_000_000,
        ts_init=SENT_MS * 1_000_000,
        strategy_id="live",
        symbol="BTC",
        seq=1,
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("42000"),
    )


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
        cut_part_wait_seconds=30.0,
    )


def test_an_order_the_venue_accepts_after_the_saga_failed_is_cancelled() -> None:
    cloid = derive_cloid(_signal().signal_id)

    async def main() -> tuple[SQLiteStore, FakeExchangeApi, PassResult]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=SENT_MS * 1_000_000)
        config = ReconcileConfig()
        post = FakeExchangeApi(
            {
                # The place request times out on our side. The venue got it.
                "order": TimeoutError("place request timed out"),
                # No record for the whole in-flight budget, then the late
                # placement lands and the venue reports it open.
                "orderStatus": answers_in_turn(
                    *[{"status": "unknownOid"}] * config.inflight_max_attempts,
                    order_status_response(cloid=cloid, oid=STRAY_OID, placed_ms=LANDED_MS),
                ),
                "userFillsByTime": [],
                "frontendOpenOrders": [{"coin": "BTC", "oid": STRAY_OID, "cloid": cloid}],
                "cancel": {
                    "status": "ok",
                    "response": {"type": "cancel", "data": {"statuses": ["success"]}},
                },
            }
        )
        exchange = _make_exchange(post, bus, clock)
        store = SQLiteStore(":memory:")
        checks = checkpointer(store, clock=clock)
        manager = ExecutionManager(bus=bus, exchange=exchange, checkpointer=checks)
        bus.subscribe(ExecutionReport, manager.on_execution_report)
        reconciler = Reconciler(
            bus=bus,
            clock=clock,
            exchange=exchange,
            account=exchange,
            cache=checks.cache,
            config=config,
        )

        await manager.on_signal(_signal())
        for _ in range(config.inflight_max_attempts):
            await reconciler.reconcile_inflight()
        failed = store.get_order(cloid)
        assert failed is not None and failed.state is OrderState.FAILED

        clock.advance_to((LANDED_MS + 30_000) * 1_000_000)
        return store, post, await reconciler.reconcile_open_orders()

    with capture_events() as events:
        store, post, result = asyncio.run(main())

    assert result is PassResult.PROVED
    cancels = [q for url, q in post.requests if request_type(url, q) == "cancel"]
    assert [c["action"]["cancels"] for c in cancels] == [[{"a": 3, "o": STRAY_OID}]]

    # The saga stays terminal. Nothing reopened it.
    after = store.get_order(cloid)
    assert after is not None and after.state is OrderState.FAILED

    strays = [e for e in events if e["event"] == "reconcile.stray_cancelled"]
    assert len(strays) == 1
    assert strays[0]["saga_state"] == OrderState.FAILED.value
    assert strays[0]["venue_oid"] == str(STRAY_OID)


def _one_open_order_pass(
    state: OrderState, *, status: str = "open", placed_ms: int = LANDED_MS
) -> tuple[FakeExchangeApi, PassResult, list[dict]]:
    """One open-order pass over a saga in ``state`` while the venue lists its cloid.

    ``status`` and ``placed_ms`` shape the record the confirm read by the
    saga's own ref answers with.
    """
    signal = _signal()
    cloid = derive_cloid(signal.signal_id)
    confirm = order_status_response(cloid=cloid, status=status, oid=STRAY_OID, placed_ms=placed_ms)

    async def main() -> tuple[FakeExchangeApi, PassResult]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=(LANDED_MS + 30_000) * 1_000_000)
        post = FakeExchangeApi(
            {
                "orderStatus": confirm,
                "userFillsByTime": [],
                "frontendOpenOrders": [{"coin": "BTC", "oid": STRAY_OID, "cloid": cloid}],
                "cancel": {
                    "status": "ok",
                    "response": {"type": "cancel", "data": {"statuses": ["success"]}},
                },
            }
        )
        exchange = _make_exchange(post, bus, clock)
        store = SQLiteStore(":memory:")
        saga = Order(
            cloid=cloid,
            strategy_id=signal.strategy_id,
            signal_id=signal.signal_id,
            symbol="BTC",
            side=Side.BUY,
            quantity=signal.quantity,
            order_type=OrderType.LIMIT,
            state=state,
            created_ts_ns=SENT_MS * 1_000_000,
        )
        store.checkpoint(saga, ts_ns=SENT_MS * 1_000_000)
        checks = checkpointer(store, clock=clock)
        checks.cache.rebuild()
        reconciler = Reconciler(
            bus=bus,
            clock=clock,
            exchange=exchange,
            account=exchange,
            cache=checks.cache,
            config=ReconcileConfig(),
        )
        return post, await reconciler.reconcile_open_orders()

    with capture_events() as events:
        post, result = asyncio.run(main())
    cancels = [
        q["action"]["cancels"] for url, q in post.requests if request_type(url, q) == "cancel"
    ]
    named = ("reconcile.stray_cancelled", "reconcile.external_order")
    return post, result, [*cancels, *(e for e in events if e["event"] in named)]


@pytest.mark.parametrize("state", [OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED])
def test_an_order_resting_under_any_sent_terminal_saga_is_cancelled(state: OrderState) -> None:
    # The saga reached the venue, so a resting order under its cloid is ours.
    _, result, seen = _one_open_order_pass(state)

    assert result is PassResult.PROVED
    cancel, stray = seen
    assert cancel == [{"a": 3, "o": STRAY_OID}]
    assert stray["saga_state"] == state.value


def test_an_order_under_a_denied_cloid_is_external_and_not_cancelled() -> None:
    # A DENIED saga never left the engine, so the venue order is not ours to cancel.
    _, result, seen = _one_open_order_pass(OrderState.DENIED)

    assert result is PassResult.PROVED
    assert seen == []


@pytest.mark.parametrize("status", ["filled", "canceled"])
def test_no_cancel_when_the_confirm_read_says_the_order_no_longer_rests(status: str) -> None:
    # The list is a snapshot and can be stale by the time we act on it. The
    # confirm read is the proof, so an order it says is gone gets no cancel.
    _, result, seen = _one_open_order_pass(OrderState.FAILED, status=status)

    assert result is PassResult.PROVED
    assert seen == []


def test_a_listed_order_placed_before_the_saga_is_external_and_not_cancelled() -> None:
    # The cloid matches, but the venue placed this order 5 minutes before the
    # saga existed. It belongs to an earlier life, not to this saga (#354).
    before_saga_ms = SENT_MS - 300_000
    _, result, seen = _one_open_order_pass(OrderState.FAILED, placed_ms=before_saga_ms)

    assert result is PassResult.PROVED
    [external] = seen
    assert external["event"] == "reconcile.external_order"
    assert external["venue_oid"] == str(STRAY_OID)
