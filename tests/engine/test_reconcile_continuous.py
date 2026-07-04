"""``Reconciler`` continuous loops — the running correctness net (ADR-0011).

After the startup barrier clears, two periodic cycles keep local saga state
converged on venue truth: a fast in-flight check resolving ``SUBMITTED`` orders
that never acked, and a slower open-order/ghost reconcile for resting orders.
Heals ride the same reconciliation-flagged synthetic events as startup, routed
through the ``ExecutionManager`` so dedup makes every cycle idempotent.
"""

import asyncio
from decimal import Decimal

import structlog.testing

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AggressorSide,
    ExecutionReport,
    FillReport,
    MarketTick,
    Order,
    OrderEvent,
    OrderFailed,
    OrderFilled,
    OrderLive,
    OrderRejected,
    OrderState,
    OrderType,
    PlaceOrder,
    Side,
    Signal,
    TimeInForce,
    VenueOrderView,
)
from tickwright.engine.cache import Cache
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.reconcile import ReconcileConfig, Reconciler


def _tick(price: str, ts: int = 1_000) -> MarketTick:
    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol="BTC",
        price=Decimal(price),
        size=Decimal("5"),
        aggressor_side=AggressorSide.SELL,
        trade_id=f"t{ts}",
        seq=0,
    )


def _resting_limit(cloid: str, price: str = "41000") -> PlaceOrder:
    return PlaceOrder(
        cloid=cloid,
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal(price),
    )


def _saga(cloid: str, state: OrderState) -> Order:
    order = Order(
        cloid=cloid,
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.LIMIT,
    )
    order.state = state
    return order


def _surviving_venue(clock: ManualClock) -> tuple[PaperExchange, InMemoryBus]:
    """A venue whose acks we never heard: its bus has no engine listeners."""
    dead_bus = InMemoryBus()
    exchange = PaperExchange(bus=dead_bus, clock=clock, fill_model=ImmediateFillModel())
    dead_bus.subscribe(MarketTick, exchange.on_tick)
    return exchange, dead_bus


def _engine(
    store: SQLiteStore,
    exchange: PaperExchange,
    clock: ManualClock,
    config: ReconcileConfig | None = None,
) -> tuple[Cache, Reconciler, list[OrderEvent]]:
    """Running-engine wiring: Cache projection, manager, the Reconciler."""
    bus = InMemoryBus()
    cache = Cache(store=store)
    cache.rebuild()
    manager = ExecutionManager(bus=bus, clock=clock, exchange=exchange, cache=cache)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    events: list[OrderEvent] = []
    bus.subscribe(OrderEvent, lambda ev: _record(events, ev))
    reconciler = Reconciler(bus=bus, clock=clock, exchange=exchange, cache=cache, config=config)
    return cache, reconciler, events


async def _record(sink: list[OrderEvent], event: OrderEvent) -> None:
    sink.append(event)


class _FlakyLink:
    """A real venue behind a link that can drop: while down, every read fails
    (``None``) — never to be confused with a venue that answers "no record"."""

    def __init__(self, venue: PaperExchange) -> None:
        self._venue = venue
        self.down = False

    async def place(self, order: PlaceOrder) -> None:
        await self._venue.place(order)

    async def cancel(self, cloid: str) -> None:
        await self._venue.cancel(cloid)

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        if self.down:
            return None
        return await self._venue.fetch_order(cloid)


# --- Fast in-flight cycle -----------------------------------------------------


def test_inflight_cycle_adopts_venue_truth_for_a_submitted_order_the_ack_missed() -> None:
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    exchange, dead_bus = _surviving_venue(clock)

    async def ack_lost() -> None:
        # The venue accepted the order and reported it LIVE, but the ack was
        # dropped in transit: our durable truth is stuck at SUBMITTED.
        await dead_bus.publish(_tick("42000"))
        await exchange.place(_resting_limit("0xabc"))

    asyncio.run(ack_lost())
    store.checkpoint(_saga("0xabc", OrderState.SUBMITTED), ts_ns=500)
    _, reconciler, events = _engine(store, exchange, clock)

    assert asyncio.run(reconciler.reconcile_inflight()) is True

    # The riskiest "did it land?" gap closed without waiting for the slow loop.
    recovered = store.get_order("0xabc")
    assert recovered is not None
    assert recovered.state is OrderState.LIVE
    lives = [ev for ev in events if isinstance(ev, OrderLive)]
    assert len(lives) == 1
    assert lives[0].reconciliation is True


def test_inflight_no_record_resolves_failed_only_after_the_attempt_budget() -> None:
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    # The venue is reachable but has never seen this order: the send is lost
    # somewhere in flight, or still on the wire.
    exchange, _ = _surviving_venue(clock)

    store.checkpoint(_saga("0xabc", OrderState.SUBMITTED), ts_ns=500)
    config = ReconcileConfig(inflight_max_attempts=3)
    _, reconciler, events = _engine(store, exchange, clock, config)

    async def cycles(n: int) -> None:
        for _ in range(n):
            assert await reconciler.reconcile_inflight() is True

    # Bounded retries (ADR-0011): a no-record read is not instant proof while
    # the send may still land — the first misses resolve nothing.
    asyncio.run(cycles(2))
    order = store.get_order("0xabc")
    assert order is not None
    assert order.state is OrderState.SUBMITTED
    assert events == []

    # The budget-exhausting miss is the proof: FAILED, never a blind resend.
    asyncio.run(cycles(1))
    order = store.get_order("0xabc")
    assert order is not None
    assert order.state is OrderState.FAILED
    assert [type(ev) for ev in events] == [OrderFailed]
    assert events[0].reconciliation is True


# --- Slow open-order / ghost cycle ----------------------------------------------


class _ForgetfulVenue:
    """A venue that answers every read positively but has lost this order —
    e.g. it expired the record. Placing through it is a test failure."""

    def __init__(self) -> None:
        self.views: dict[str, VenueOrderView] = {}

    async def place(self, order: PlaceOrder) -> None:
        raise AssertionError("the ghost cycle must never place")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("the ghost cycle must never cancel")

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        return self.views.get(cloid, VenueOrderView(status=None))


def test_a_live_order_absent_across_the_grace_window_resolves_rejected() -> None:
    clock = ManualClock(start_ns=0)
    store = SQLiteStore(":memory:")
    store.checkpoint(_saga("0xabc", OrderState.LIVE), ts_ns=500)
    venue = _ForgetfulVenue()
    config = ReconcileConfig(ghost_grace_seconds=90.0)
    _, reconciler, events = _engine(store, venue, clock, config)  # type: ignore[arg-type]

    async def scenario() -> None:
        # First sighting of the absence arms the grace clock; well inside the
        # window a missing record proves nothing (ADR-0011 inv 3).
        assert await reconciler.reconcile_open_orders() is True
        await clock.sleep(50.0)
        assert await reconciler.reconcile_open_orders() is True
        order = store.get_order("0xabc")
        assert order is not None
        assert order.state is OrderState.LIVE

        # Continuously absent past the window: truly gone → REJECTED from LIVE
        # (ADR-0010), announced as a ghost resolution.
        await clock.sleep(41.0)
        with structlog.testing.capture_logs() as logs:
            assert await reconciler.reconcile_open_orders() is True
        order = store.get_order("0xabc")
        assert order is not None
        assert order.state is OrderState.REJECTED
        ghosts = [log for log in logs if log["event"] == "ghost.reconciled"]
        assert len(ghosts) == 1
        assert ghosts[0]["resolution"] == "rejected"

    asyncio.run(scenario())
    rejected = [ev for ev in events if isinstance(ev, OrderRejected)]
    assert len(rejected) == 1
    assert rejected[0].reconciliation is True


def test_a_ghost_whose_fill_history_has_fills_heals_to_filled_not_rejected() -> None:
    clock = ManualClock(start_ns=0)
    store = SQLiteStore(":memory:")
    store.checkpoint(_saga("0xabc", OrderState.LIVE), ts_ns=500)
    venue = _ForgetfulVenue()
    # The open-order record is gone, but fill history remembers: the order
    # executed. The cross-check is mandatory before any "gone" verdict
    # (ADR-0011 inv 2/4) — and executed truth needs no grace wait.
    venue.views["0xabc"] = VenueOrderView(
        status=None,
        fills=(
            FillReport(
                ts_event=800,
                ts_init=800,
                cloid="0xabc",
                symbol="BTC",
                trade_id="0xabc-1",
                quantity=Decimal("0.5"),
                price=Decimal("41000"),
            ),
        ),
    )
    _, reconciler, events = _engine(store, venue, clock)  # type: ignore[arg-type]

    assert asyncio.run(reconciler.reconcile_open_orders()) is True

    order = store.get_order("0xabc")
    assert order is not None
    assert order.state is OrderState.FILLED
    assert order.cum_qty == Decimal("0.5")
    filled = [ev for ev in events if isinstance(ev, OrderFilled)]
    assert len(filled) == 1
    assert filled[0].reconciliation is True
    assert not [ev for ev in events if isinstance(ev, OrderRejected)]


# --- Connectivity guard ---------------------------------------------------------


def test_a_none_read_freezes_the_cycle_emits_frozen_and_the_next_cycle_heals() -> None:
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    exchange, dead_bus = _surviving_venue(clock)

    async def ack_lost() -> None:
        await dead_bus.publish(_tick("42000"))
        await exchange.place(_resting_limit("0xabc"))

    asyncio.run(ack_lost())
    store.checkpoint(_saga("0xabc", OrderState.SUBMITTED), ts_ns=500)
    link = _FlakyLink(exchange)
    _, reconciler, events = _engine(store, link, clock)  # type: ignore[arg-type]

    # An outage must never read as "all orders vanished" (ADR-0011 inv 1):
    # the cycle freezes, resolves nothing, and says so as telemetry.
    link.down = True
    with structlog.testing.capture_logs() as logs:
        assert asyncio.run(reconciler.reconcile_inflight()) is False
    order = store.get_order("0xabc")
    assert order is not None
    assert order.state is OrderState.SUBMITTED
    assert events == []
    frozen = [log for log in logs if log["event"] == "reconcile.frozen"]
    assert len(frozen) == 1
    assert frozen[0]["cycle"] == "inflight"

    # The link returns: the very next cycle heals normally — the freeze also
    # never advanced the no-record miss count toward a false FAILED.
    link.down = False
    assert asyncio.run(reconciler.reconcile_inflight()) is True
    order = store.get_order("0xabc")
    assert order is not None
    assert order.state is OrderState.LIVE
