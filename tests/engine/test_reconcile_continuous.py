"""``Reconciler`` continuous loops — the running correctness net (ADR-0011).

After the startup barrier clears, two periodic cycles keep local saga state
converged on venue truth: a fast in-flight check resolving ``SUBMITTED`` orders
that never acked, and a slower open-order/ghost reconcile for resting orders.
Heals ride the same reconciliation-flagged synthetic events as startup, routed
through the ``ExecutionManager`` so dedup makes every cycle idempotent.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from decimal import Decimal

import pytest
import structlog.testing
from hypothesis import given
from hypothesis import strategies as st

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AggressorSide,
    Exchange,
    ExecutionReport,
    FillReport,
    InstrumentSpec,
    MarketTick,
    Order,
    OrderCancelled,
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
from tickwright.observability.testing import capture_events


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
    return exchange, dead_bus


def _wire(
    store: SQLiteStore,
    exchange: Exchange,
    clock: ManualClock,
    config: ReconcileConfig | None = None,
) -> tuple[InMemoryBus, Reconciler, list[OrderEvent], Cache]:
    """The shared running-engine wiring: a Cache projection, the ExecutionManager,
    and the Reconciler on one bus, with an OrderEvent sink."""
    bus = InMemoryBus()
    cache = Cache(store=store)
    cache.rebuild()
    manager = ExecutionManager(bus=bus, clock=clock, exchange=exchange, cache=cache)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    events: list[OrderEvent] = []
    bus.subscribe(OrderEvent, lambda ev: _record(events, ev))
    reconciler = Reconciler(
        bus=bus,
        clock=clock,
        exchange=exchange,
        cache=cache,
        config=config if config is not None else ReconcileConfig(),
    )
    return bus, reconciler, events, cache


def _engine(
    store: SQLiteStore,
    exchange: PaperExchange,
    clock: ManualClock,
    config: ReconcileConfig | None = None,
) -> tuple[InMemoryBus, Reconciler, list[OrderEvent]]:
    """Running-engine wiring: Cache projection, manager, the Reconciler."""
    bus, reconciler, events, _ = _wire(store, exchange, clock, config)
    return bus, reconciler, events


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


def test_the_inflight_failed_verdict_is_announced_as_a_named_event() -> None:
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    exchange, _ = _surviving_venue(clock)

    store.checkpoint(_saga("0xabc", OrderState.SUBMITTED), ts_ns=500)
    config = ReconcileConfig(inflight_max_attempts=2)
    _, reconciler, _ = _engine(store, exchange, clock, config)

    # The first miss proves nothing and stays silent; only the budget-exhausting
    # verdict is a reconcile decision worth its own telemetry — parity with the
    # ghost cadence's ``ghost.reconciled`` (ADR-0020).
    with structlog.testing.capture_logs() as first:
        assert asyncio.run(reconciler.reconcile_inflight()) is True
    assert not [log for log in first if log["event"] == "inflight.reconciled"]

    with capture_events() as verdict:
        assert asyncio.run(reconciler.reconcile_inflight()) is True
    reconciled = [log for log in verdict if log["event"] == "inflight.reconciled"]
    assert len(reconciled) == 1
    # The cloid rides the ambient reconcile context, not the call site (ADR-0020).
    assert reconciled[0]["cloid"] == "0xabc"
    assert reconciled[0]["resolution"] == "failed"


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

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        return {}


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


def test_a_healthy_resting_order_makes_the_slow_cycle_a_quiet_no_op() -> None:
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    exchange, dead_bus = _surviving_venue(clock)

    async def resting() -> None:
        await dead_bus.publish(_tick("42000"))
        await exchange.place(_resting_limit("0xabc"))

    asyncio.run(resting())
    # This life heard the LIVE ack: local and venue truth already agree, and
    # the saga's dedup set reflects the applied LIVE transition.
    saga = _saga("0xabc", OrderState.SUBMITTED)
    saga.apply(
        OrderLive(
            ts_event=600,
            ts_init=600,
            cloid="0xabc",
            strategy_id="trivial",
            signal_id="trivial:BTC:1",
            symbol="BTC",
        )
    )
    store.checkpoint(saga, ts_ns=700)
    _, reconciler, events = _engine(store, exchange, clock)

    async def cycles() -> None:
        for _ in range(3):
            assert await reconciler.reconcile_open_orders() is True

    asyncio.run(cycles())

    # Convergence means silence: the replayed LIVE dedups by its
    # state-deterministic event_id, and nothing is ever republished.
    order = store.get_order("0xabc")
    assert order is not None
    assert order.state is OrderState.LIVE
    assert events == []


def test_a_healed_fill_and_the_venues_late_duplicate_collapse_to_one_apply() -> None:
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    exchange, dead_bus = _surviving_venue(clock)

    async def degraded_link() -> None:
        # The venue accepted the order and later filled it, but both reports
        # died on a degraded link: locally the order still looks LIVE.
        await dead_bus.publish(_tick("42000"))
        await exchange.place(_resting_limit("0xabc"))
        await dead_bus.publish(_tick("40000", ts=1_500))

    asyncio.run(degraded_link())
    store.checkpoint(_saga("0xabc", OrderState.LIVE), ts_ns=500)
    engine_bus, reconciler, events = _engine(store, exchange, clock)

    # The slow cycle heals the missed fill through the ExecutionManager.
    assert asyncio.run(reconciler.reconcile_open_orders()) is True
    order = store.get_order("0xabc")
    assert order is not None
    assert order.state is OrderState.FILLED
    assert order.cum_qty == Decimal("0.5")

    # The venue's own late copy of the same fill finally arrives. The healed
    # replica shares its event_id — provenance is excluded from the dedup key
    # (ADR-0025) — so the duplicate collapses: applied once, republished never.
    venue_view = asyncio.run(exchange.fetch_order("0xabc"))
    assert venue_view is not None
    (venue_fill,) = venue_view.fills
    healed_twin = replace(venue_fill, reconciliation=True)
    assert healed_twin.event_id == venue_fill.event_id

    asyncio.run(engine_bus.publish(venue_fill))

    order = store.get_order("0xabc")
    assert order is not None
    assert order.cum_qty == Decimal("0.5")  # never double-counted
    filled = [ev for ev in events if isinstance(ev, OrderFilled)]
    assert len(filled) == 1


def test_a_vanished_order_with_the_cancel_requested_marker_resolves_cancelled() -> None:
    clock = ManualClock(start_ns=0)
    store = SQLiteStore(":memory:")
    # A cancel was durably requested and sent, then the ack was lost and the
    # venue's record expired: the marker is the durable memory of our own
    # intent (ADR-0026), so the vanish reads as the cancel landing.
    saga = _saga("0xabc", OrderState.LIVE)
    assert saga.request_cancel(signal_id="trivial:BTC:2", ts_ns=400)
    store.checkpoint(saga, ts_ns=500)
    venue = _ForgetfulVenue()
    config = ReconcileConfig(ghost_grace_seconds=90.0)
    _, reconciler, events = _engine(store, venue, clock, config)  # type: ignore[arg-type]

    async def scenario() -> None:
        assert await reconciler.reconcile_open_orders() is True
        await clock.sleep(91.0)
        with structlog.testing.capture_logs() as logs:
            assert await reconciler.reconcile_open_orders() is True
        ghosts = [log for log in logs if log["event"] == "ghost.reconciled"]
        assert len(ghosts) == 1
        assert ghosts[0]["resolution"] == "cancelled"

    asyncio.run(scenario())
    order = store.get_order("0xabc")
    assert order is not None
    assert order.state is OrderState.CANCELLED
    cancelled = [ev for ev in events if isinstance(ev, OrderCancelled)]
    assert len(cancelled) == 1
    assert cancelled[0].reconciliation is True
    assert not [ev for ev in events if isinstance(ev, OrderRejected)]


def test_a_partially_filled_ghost_truly_gone_resolves_cancelled_with_fills_kept() -> None:
    clock = ManualClock(start_ns=0)
    store = SQLiteStore(":memory:")
    # Half the order executed, then the venue's record vanished for good.
    saga = _saga("0xabc", OrderState.PARTIALLY_FILLED)
    saga.cum_qty = Decimal("0.2")
    store.checkpoint(saga, ts_ns=500)
    venue = _ForgetfulVenue()
    config = ReconcileConfig(ghost_grace_seconds=90.0)
    _, reconciler, events = _engine(store, venue, clock, config)  # type: ignore[arg-type]

    async def scenario() -> None:
        assert await reconciler.reconcile_open_orders() is True
        await clock.sleep(91.0)
        assert await reconciler.reconcile_open_orders() is True

    asyncio.run(scenario())
    # CANCELLED, not REJECTED: a rejection would deny fills that provably
    # happened — the executed quantity is preserved (ADR-0010/0011).
    order = store.get_order("0xabc")
    assert order is not None
    assert order.state is OrderState.CANCELLED
    assert order.cum_qty == Decimal("0.2")
    cancelled = [ev for ev in events if isinstance(ev, OrderCancelled)]
    assert len(cancelled) == 1
    assert cancelled[0].reconciliation is True


# --- Recent-order protection window (ADR-0011 inv 3) ----------------------------


def _running_engine(
    store: SQLiteStore,
    venue: Exchange,
    clock: ManualClock,
    config: ReconcileConfig | None = None,
) -> tuple[Reconciler, list[OrderEvent], Cache]:
    """Engine wiring that also hands back the ``Cache``, so a test can checkpoint
    a just-acked saga through the write-through path at a controlled ``now`` — the
    venue's open-orders snapshot then lags that ack."""
    _, reconciler, events, cache = _wire(store, venue, clock, config)
    return reconciler, events, cache


def test_a_recent_orders_absence_neither_arms_the_grace_clock_nor_ghosts_it() -> None:
    clock = ManualClock(start_ns=1_000_000_000)
    store = SQLiteStore(":memory:")
    venue = _ForgetfulVenue()  # the venue's open-orders snapshot has not propagated the ack yet
    config = ReconcileConfig(recent_order_protection_seconds=30.0, ghost_grace_seconds=90.0)
    reconciler, events, cache = _running_engine(store, venue, clock, config)
    # A LIVE ack we just heard and checkpointed: local truth is fresh as of now.
    cache.checkpoint(_saga("0xabc", OrderState.LIVE), ts_ns=clock.timestamp_ns())

    # Well inside the protection window a missing venue record proves nothing —
    # the ghost cycle skips it entirely rather than racing the venue (ADR-0011
    # inv 3): the grace clock never arms and the skip is named for auditability.
    async def scenario() -> None:
        for _ in range(3):
            with capture_events() as logs:
                assert await reconciler.reconcile_open_orders() is True
            skipped = [log for log in logs if log["event"] == "reconcile.recency_skipped"]
            assert len(skipped) == 1
            assert skipped[0]["cloid"] == "0xabc"
            await clock.sleep(5.0)

    asyncio.run(scenario())
    order = store.get_order("0xabc")
    assert order is not None
    assert order.state is OrderState.LIVE
    assert events == []


def test_a_recent_order_that_filled_heals_immediately_inside_the_protection_window() -> None:
    clock = ManualClock(start_ns=1_000_000_000)
    store = SQLiteStore(":memory:")
    venue = _ForgetfulVenue()
    # The open-orders snapshot has not propagated the ack, but fill history
    # already remembers the order executed in full.
    venue.views["0xabc"] = VenueOrderView(
        status=None,
        fills=(
            FillReport(
                ts_event=1_000_000_100,
                ts_init=1_000_000_100,
                cloid="0xabc",
                symbol="BTC",
                trade_id="0xabc-1",
                quantity=Decimal("0.5"),
                price=Decimal("41000"),
            ),
        ),
    )
    config = ReconcileConfig(recent_order_protection_seconds=30.0)
    reconciler, events, cache = _running_engine(store, venue, clock, config)
    cache.checkpoint(_saga("0xabc", OrderState.LIVE), ts_ns=clock.timestamp_ns())

    # The order is well within its protection window, but the fill-history
    # cross-check is mandatory and independent of the recency skip (ADR-0011
    # inv 2/4): executed truth heals immediately, with no grace wait.
    assert asyncio.run(reconciler.reconcile_open_orders()) is True

    order = store.get_order("0xabc")
    assert order is not None
    assert order.state is OrderState.FILLED
    assert order.cum_qty == Decimal("0.5")
    filled = [ev for ev in events if isinstance(ev, OrderFilled)]
    assert len(filled) == 1
    assert filled[0].reconciliation is True


def test_a_recent_order_resumes_normal_ghost_resolution_once_it_ages_out() -> None:
    clock = ManualClock(start_ns=1_000_000_000)
    store = SQLiteStore(":memory:")
    venue = _ForgetfulVenue()
    config = ReconcileConfig(recent_order_protection_seconds=30.0, ghost_grace_seconds=90.0)
    reconciler, events, cache = _running_engine(store, venue, clock, config)
    cache.checkpoint(_saga("0xabc", OrderState.LIVE), ts_ns=clock.timestamp_ns())

    async def scenario() -> None:
        # Inside the protection window: skipped, so the grace clock never armed.
        assert await reconciler.reconcile_open_orders() is True
        # Aged past protection: the first unprotected sighting arms the grace
        # clock now (never earlier), so a lone read still ghosts nothing.
        await clock.sleep(31.0)
        assert await reconciler.reconcile_open_orders() is True
        order = store.get_order("0xabc")
        assert order is not None
        assert order.state is OrderState.LIVE
        # Continuously absent across the grace window past that arming: the #15
        # ghost behavior resumes unchanged → REJECTED from LIVE (ADR-0010/0011).
        await clock.sleep(91.0)
        assert await reconciler.reconcile_open_orders() is True

    asyncio.run(scenario())
    order = store.get_order("0xabc")
    assert order is not None
    assert order.state is OrderState.REJECTED
    rejected = [ev for ev in events if isinstance(ev, OrderRejected)]
    assert len(rejected) == 1
    assert rejected[0].reconciliation is True


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
    with capture_events() as logs:
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


@pytest.mark.parametrize(
    ("cycle_method", "saga_state", "frozen_label"),
    [
        ("reconcile_startup", OrderState.SUBMITTED, "startup"),
        ("reconcile_inflight", OrderState.SUBMITTED, "inflight"),
        ("reconcile_open_orders", OrderState.LIVE, "open_order"),
    ],
)
def test_every_cadence_freezes_on_a_none_read_and_removes_nothing(
    cycle_method: str, saga_state: OrderState, frozen_label: str
) -> None:
    # The connectivity guard (ADR-0011 inv 1) is one skeleton behind every
    # cadence: a failed read freezes the whole pass, resolves nothing, and names
    # the frozen cycle — so no cadence can silently read an outage as "gone".
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    exchange, _ = _surviving_venue(clock)
    store.checkpoint(_saga("0xabc", saga_state), ts_ns=500)
    link = _FlakyLink(exchange)
    _, reconciler, events = _engine(store, link, clock)  # type: ignore[arg-type]

    link.down = True
    with capture_events() as logs:
        assert asyncio.run(getattr(reconciler, cycle_method)()) is False

    order = store.get_order("0xabc")
    assert order is not None
    assert order.state is saga_state
    assert events == []
    frozen = [log for log in logs if log["event"] == "reconcile.frozen"]
    assert len(frozen) == 1
    assert frozen[0]["cycle"] == frozen_label


# --- Timing rule (ADR-0008/0011) --------------------------------------------------


@given(
    interval=st.floats(min_value=0.1, max_value=60.0),
    attempts=st.integers(min_value=1, max_value=100),
    slow_interval=st.floats(min_value=1.0, max_value=300.0),
    grace=st.floats(min_value=0.1, max_value=600.0),
    protection=st.floats(min_value=0.1, max_value=600.0),
)
def test_every_constructible_config_respects_the_ghost_grace_timing_bounds(
    interval: float, attempts: int, slow_interval: float, grace: float, protection: float
) -> None:
    # Both sub-windows must sit under the ghost grace window, enforced at
    # construction so no runtime path ever holds a config where an order still
    # being retried could be ghosted, nor one where the protection pre-filter
    # outlasts the grace measurement it precedes (ADR-0008/0011).
    try:
        config = ReconcileConfig(
            inflight_interval_seconds=interval,
            inflight_max_attempts=attempts,
            open_order_interval_seconds=slow_interval,
            ghost_grace_seconds=grace,
            recent_order_protection_seconds=protection,
        )
    except ValueError:
        assert interval * attempts >= grace or protection >= grace
        return
    budget = config.inflight_interval_seconds * config.inflight_max_attempts
    assert budget < config.ghost_grace_seconds
    assert config.recent_order_protection_seconds < config.ghost_grace_seconds


def test_a_config_whose_retry_budget_reaches_the_ghost_grace_is_rejected() -> None:
    with pytest.raises(ValueError, match="ghost grace"):
        ReconcileConfig(
            inflight_interval_seconds=30.0, inflight_max_attempts=3, ghost_grace_seconds=90.0
        )


def test_a_config_whose_protection_window_reaches_the_ghost_grace_is_rejected() -> None:
    # The protection window is a brief pre-filter that defers the *start* of the
    # grace measurement for a just-acked order; the grace window is the
    # substantive continuous-absence measurement. A protection window that reaches
    # the grace window inverts that layering — the shield outlasts the measurement
    # it precedes — so it is rejected as a misconfiguration (ADR-0011 inv 3).
    with pytest.raises(ValueError, match="protection window"):
        ReconcileConfig(recent_order_protection_seconds=90.0, ghost_grace_seconds=90.0)


@pytest.mark.parametrize(
    "field",
    [
        "inflight_interval_seconds",
        "inflight_max_attempts",
        "open_order_interval_seconds",
        "ghost_grace_seconds",
        "recent_order_protection_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_a_config_with_a_non_positive_timing_knob_is_rejected(field: str, value: int) -> None:
    # Every timing knob must be strictly positive; the guard names the offending
    # field so a misconfiguration is diagnosable. Checked before the budget rule,
    # so a zero ghost_grace_seconds fails positivity, not the budget comparison.
    kwargs: dict[str, int] = {field: value}
    with pytest.raises(ValueError, match=f"{field} must be positive"):
        ReconcileConfig(**kwargs)
