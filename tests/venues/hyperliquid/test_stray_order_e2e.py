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
    fill_entry,
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
    VenueReadUnresolvable,
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
from tickwright.venues.hyperliquid.transport import IpRateLimited

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


def _open_order_passes(
    state: OrderState,
    *,
    status: str = "open",
    placed_ms: int = LANDED_MS,
    listed_cloid: str | None = None,
    fills: list[dict] | None = None,
    passes: int = 1,
    open_orders: object = None,
    confirm_answer: object = None,
    interval_seconds: int = 0,
) -> tuple[Order | None, list[PassResult], list[dict]]:
    """Open-order passes over a saga in ``state`` while the venue lists one order.

    ``status`` and ``placed_ms`` shape the record the confirm read by the
    saga's own ref answers with, and ``fills`` its fill history. The listed
    order carries the saga's cloid, unless ``listed_cloid`` names another one.
    An empty string lists it with no cloid at all, as an order placed by hand.
    ``open_orders`` replaces the list answer, such as with a failure, and
    ``confirm_answer`` replaces the confirm read's answer the same way. The clock
    moves ``interval_seconds`` after each pass, as the cadence would.

    Returns the saga as stored after the passes, each pass result, and the
    cancels sent followed by the stray, external and frozen log lines.
    """
    signal = _signal()
    cloid = derive_cloid(signal.signal_id)
    confirm = order_status_response(cloid=cloid, status=status, oid=STRAY_OID, placed_ms=placed_ms)
    listed = cloid if listed_cloid is None else (listed_cloid or None)

    async def main() -> tuple[SQLiteStore, FakeExchangeApi, list[PassResult]]:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=(LANDED_MS + 30_000) * 1_000_000)
        post = FakeExchangeApi(
            {
                "orderStatus": confirm if confirm_answer is None else confirm_answer,
                "userFillsByTime": fills or [],
                "frontendOpenOrders": (
                    [{"coin": "BTC", "oid": STRAY_OID, "cloid": listed}]
                    if open_orders is None
                    else open_orders
                ),
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
        results = []
        for _ in range(passes):
            results.append(await reconciler.reconcile_open_orders())
            clock.advance_to(clock.timestamp_ns() + interval_seconds * 1_000_000_000)
        return store, post, results

    with capture_events() as events:
        store, post, results = asyncio.run(main())
    cancels = [
        q["action"]["cancels"] for url, q in post.requests if request_type(url, q) == "cancel"
    ]
    named = ("reconcile.stray_cancelled", "reconcile.external_order", "reconcile.frozen")
    return store.get_order(cloid), results, [*cancels, *(e for e in events if e["event"] in named)]


def test_a_strays_fills_are_logged_with_its_cancel_and_never_booked() -> None:
    # The stray rested long enough to trade 0.2 of its 0.5. Booking that fill
    # would move a terminal saga, an illegal transition that faults the engine.
    # Ledger reconciliation books the position as unattributed instead.
    fills = [
        fill_entry(oid=STRAY_OID, tid=9001, px="42000", sz="0.2", fee="1.68", time=LANDED_MS),
        fill_entry(oid=STRAY_OID, tid=9002, px="42000", sz="0.1", fee="0.84", time=LANDED_MS),
    ]
    saga, [result], seen = _open_order_passes(OrderState.FAILED, fills=fills)

    assert result is PassResult.PROVED
    _, stray = seen
    unbooked = {k: v for k, v in stray.items() if k.startswith("unbooked_")}
    assert unbooked == {
        "unbooked_trade_ids": "9001,9002",
        "unbooked_quantity": "0.3",
        "unbooked_fee": "2.52",
    }
    assert saga is not None
    assert (saga.state, saga.cum_qty) == (OrderState.FAILED, Decimal("0"))


@pytest.mark.parametrize("state", [OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED])
def test_an_order_resting_under_any_sent_terminal_saga_is_cancelled(state: OrderState) -> None:
    # The saga reached the venue, so a resting order under its cloid is ours.
    _, [result], seen = _open_order_passes(state)

    assert result is PassResult.PROVED
    cancel, stray = seen
    assert cancel == [{"a": 3, "o": STRAY_OID}]
    assert stray["saga_state"] == state.value


def test_an_order_under_a_denied_cloid_is_external_and_not_cancelled() -> None:
    # A DENIED saga never left the engine, so the venue order is not ours to cancel.
    _, [result], seen = _open_order_passes(OrderState.DENIED)

    assert result is PassResult.PROVED
    [external] = seen
    assert external["event"] == "reconcile.external_order"
    assert external["venue_oid"] == str(STRAY_OID)


@pytest.mark.parametrize("status", ["filled", "canceled"])
def test_no_cancel_when_the_confirm_read_says_the_order_no_longer_rests(status: str) -> None:
    # The list is a snapshot and can be stale by the time we act on it. The
    # confirm read is the proof, so an order it says is gone gets no cancel.
    _, [result], seen = _open_order_passes(OrderState.FAILED, status=status)

    assert result is PassResult.PROVED
    assert seen == []


def test_a_listed_order_placed_before_the_saga_is_external_and_not_cancelled() -> None:
    # The cloid matches, but the venue placed this order 5 minutes before the
    # saga existed. It belongs to an earlier life, not to this saga (#354).
    before_saga_ms = SENT_MS - 300_000
    _, [result], seen = _open_order_passes(OrderState.FAILED, placed_ms=before_saga_ms)

    assert result is PassResult.PROVED
    [external] = seen
    assert external["event"] == "reconcile.external_order"
    assert external["venue_oid"] == str(STRAY_OID)


@pytest.mark.parametrize(
    "listed_cloid",
    ["0x00000000000000000000000000000abc", ""],
    ids=["unknown-cloid", "no-cloid"],
)
def test_an_order_we_never_placed_is_logged_once_and_never_cancelled(listed_cloid: str) -> None:
    # Three passes see the same order. The operator needs to hear of it once,
    # not on every pass for as long as it rests.
    _, results, seen = _open_order_passes(OrderState.FAILED, listed_cloid=listed_cloid, passes=3)

    assert results == [PassResult.PROVED] * 3
    [external] = seen
    assert external["event"] == "reconcile.external_order"
    assert external["venue_oid"] == str(STRAY_OID)


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TimeoutError("list read timed out"), PassResult.FAILED),
        (IpRateLimited("429 from the venue"), PassResult.RATE_LIMITED),
    ],
    ids=["send-failed", "rate-limited"],
)
def test_a_failed_list_read_freezes_the_pass(failure: Exception, expected: PassResult) -> None:
    # Without the list the pass cannot tell a stray from nothing at all. An
    # outage must never read as "no order rests" (ADR-0011 inv 1).
    saga, [result], seen = _open_order_passes(OrderState.FAILED, open_orders=failure)

    assert result is expected
    [frozen] = seen
    assert (frozen["event"], frozen["scope"]) == ("reconcile.frozen", "cycle")
    assert saga is not None and saga.state is OrderState.FAILED


@pytest.mark.parametrize(
    ("failure", "expected", "scope"),
    [
        (TimeoutError("confirm read timed out"), PassResult.FAILED, "cycle"),
        (IpRateLimited("429 from the venue"), PassResult.RATE_LIMITED, "cycle"),
        ({"error": "not an order status"}, PassResult.FAILED, "order"),
    ],
    ids=["send-failed", "rate-limited", "unreadable"],
)
def test_a_failed_confirm_read_freezes_and_sends_no_cancel(
    failure: object, expected: PassResult, scope: str
) -> None:
    # The list alone is not proof the order still rests. Without the confirm
    # read there is nothing to cancel on, so the pass freezes the same way the
    # per-order passes do (ADR-0049). The next pass reads it again.
    saga, [result], seen = _open_order_passes(OrderState.FAILED, confirm_answer=failure)

    assert result is expected
    [frozen] = seen
    assert (frozen["event"], frozen["scope"]) == ("reconcile.frozen", scope)
    assert saga is not None and saga.state is OrderState.FAILED


UNREADABLE_LIST = {"error": "not a list"}
"""A list answer the adapter cannot read, as a truncated or changed response."""


def test_a_list_unreadable_across_the_span_faults_the_engine() -> None:
    # One unreadable list may be a truncated response. A list unreadable for
    # the whole 90s span is durable, and only an operator can resolve it. Left
    # alone, the engine would never see a stray order again (ADR-0049).
    saga, results, seen = _open_order_passes(
        OrderState.FAILED, open_orders=UNREADABLE_LIST, passes=3, interval_seconds=30
    )

    # Passes at 0s, 30s and 60s: frozen, not yet faulted.
    assert results == [PassResult.FAILED] * 3
    assert [(e["event"], e["scope"]) for e in seen] == [("reconcile.frozen", "cycle")] * 3
    assert saga is not None and saga.state is OrderState.FAILED

    # The pass at 90s spends the span.
    with pytest.raises(VenueReadUnresolvable, match="open order list"):
        _open_order_passes(
            OrderState.FAILED, open_orders=UNREADABLE_LIST, passes=4, interval_seconds=30
        )


def test_a_readable_list_restarts_the_unreadable_span() -> None:
    # The span counts continuous unreadable time. A list read in between
    # proves the condition passed, so the clock starts again from the next one.
    listed = [{"coin": "BTC", "oid": STRAY_OID, "cloid": None}]
    answers = answers_in_turn(UNREADABLE_LIST, UNREADABLE_LIST, listed, UNREADABLE_LIST)

    _, results, _ = _open_order_passes(
        OrderState.FAILED, open_orders=answers, passes=5, interval_seconds=30
    )

    # Unreadable at 0s and 30s, read at 60s, unreadable again from 90s.
    assert results == [
        PassResult.FAILED,
        PassResult.FAILED,
        PassResult.PROVED,
        PassResult.FAILED,
        PassResult.FAILED,
    ]


def test_two_listed_orders_under_one_cloid_get_one_cancel_and_one_external_line() -> None:
    # A cloid can name more than one venue order, one per life of the account
    # (#354). The confirm read names the saga's order. The other listed order
    # is not the saga's, so it is external, and the saga's gets one cancel.
    cloid = derive_cloid(_signal().signal_id)
    earlier_oid = 88
    listed = [
        {"coin": "BTC", "oid": STRAY_OID, "cloid": cloid},
        {"coin": "BTC", "oid": earlier_oid, "cloid": cloid},
    ]

    _, [result], seen = _open_order_passes(OrderState.FAILED, open_orders=listed)

    assert result is PassResult.PROVED
    cancels = [s for s in seen if isinstance(s, list)]
    assert cancels == [[{"a": 3, "o": STRAY_OID}]]
    lines = [(e["event"], e["venue_oid"]) for e in seen if isinstance(e, dict)]
    assert sorted(lines) == [
        ("reconcile.external_order", str(earlier_oid)),
        ("reconcile.stray_cancelled", str(STRAY_OID)),
    ]


def test_an_earlier_lifes_order_is_logged_once_across_passes() -> None:
    before_saga_ms = SENT_MS - 300_000
    _, _, seen = _open_order_passes(OrderState.FAILED, placed_ms=before_saga_ms, passes=3)

    assert [e["event"] for e in seen] == ["reconcile.external_order"]
