"""``Reconciler`` — the startup mass-rebuild and its barrier (ADR-0009/0011/0024).

After any death the engine rebuilds the ``Cache`` from the ``Store`` and
reconciles every non-terminal saga against the venue by cloid **before anything
can be placed**: found → adopt the venue's state through reconciliation-flagged
synthetic reports routed via the ``ExecutionManager``; positively no record →
``FAILED``, never a blind resend. The venue here is the real ``PaperExchange``
playing the role of a venue that outlived our crash — its bus from the first
life is dead air, exactly like acks that died with the process.
"""

import asyncio
from decimal import Decimal

import pytest
from ledgers import GENESIS, checkpointer
from venue_doubles import VenueDouble

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AggressorSide,
    ExecutionReport,
    MarketTick,
    Order,
    OrderEvent,
    OrderFailed,
    OrderFilled,
    OrderLive,
    OrderState,
    OrderType,
    PlaceOrder,
    Side,
    Signal,
    StartupReconciliationTimeout,
    TimeInForce,
    VenueOrderView,
)
from tickwright.engine.barrier import StartupBarrier
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
    """A venue that outlived our crash: its first-life bus has no listeners."""
    dead_bus = InMemoryBus()
    exchange = PaperExchange(
        bus=dead_bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
    )
    return exchange, dead_bus


def _second_life(
    store: SQLiteStore, exchange: PaperExchange, clock: ManualClock
) -> tuple[InMemoryBus, Cache, Reconciler, list[OrderEvent]]:
    """Recovery wiring: rebuilt Cache, fresh bus/manager, the Reconciler."""
    bus = InMemoryBus()
    checks = checkpointer(store, clock=clock)
    cache = checks.cache
    cache.rebuild()
    manager = ExecutionManager(
        bus=bus,
        exchange=exchange,
        checkpointer=checks,
    )
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    events: list[OrderEvent] = []
    bus.subscribe(OrderEvent, lambda ev: _record(events, ev))
    reconciler = Reconciler(
        bus=bus, clock=clock, exchange=exchange, cache=cache, config=ReconcileConfig()
    )
    return bus, cache, reconciler, events


async def _record(sink: list[OrderEvent], event: OrderEvent) -> None:
    sink.append(event)


def _barrier(clock: ManualClock, reconciler: Reconciler) -> StartupBarrier:
    """The gate as the runner composes it here — the mass-rebuild alone.

    The runner's own sequence orders a live-only account materialisation ahead
    of this step (ADR-0043 §6); this suite's venue is paper, whose account row
    exists before the barrier ever runs, so the step is a no-op and what is left
    is the rebuild under the gate's retry policy (``tests/engine/test_barrier.py``
    holds the policy itself, and ``test_runner_e2e.py`` the composed order).
    """
    return StartupBarrier(clock=clock, steps=(reconciler.reconcile_startup,))


def test_recovered_submitted_saga_adopts_the_venue_live_state() -> None:
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    exchange, dead_bus = _surviving_venue(clock)

    async def first_life() -> None:
        # The venue accepted the order and reported it LIVE, but that ack died
        # with the process: our durable truth stopped at SUBMITTED.
        await dead_bus.publish(_tick("42000"))
        await exchange.place(_resting_limit("0xabc"))

    asyncio.run(first_life())
    store.checkpoint(_saga("0xabc", OrderState.SUBMITTED), ts_ns=500)

    _, cache, reconciler, events = _second_life(store, exchange, clock)

    assert asyncio.run(reconciler.reconcile_startup()) is True

    # The saga converged on venue truth — durably, not just in memory.
    recovered = store.get_order("0xabc")
    assert recovered is not None
    assert recovered.state is OrderState.LIVE
    lives = [ev for ev in events if isinstance(ev, OrderLive)]
    assert len(lives) == 1
    # The heal is auditable as reconciler-sourced (ADR-0011 inv 6).
    assert lives[0].reconciliation is True


def test_recovered_submitted_saga_the_venue_never_saw_resolves_failed_not_resent() -> None:
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    # The venue outlived our crash but never received this order: the crash
    # happened inside the send window, before the request landed.
    exchange, _ = _surviving_venue(clock)

    store.checkpoint(_saga("0xabc", OrderState.SUBMITTED), ts_ns=500)
    _, _, reconciler, events = _second_life(store, exchange, clock)

    assert asyncio.run(reconciler.reconcile_startup()) is True

    # Proven non-landing → FAILED (ADR-0010/0011), durably.
    recovered = store.get_order("0xabc")
    assert recovered is not None
    assert recovered.state is OrderState.FAILED
    assert [type(ev) for ev in events] == [OrderFailed]
    assert events[0].reconciliation is True
    # Never blind-resent (ADR-0008 rule 2): the venue still has no record.
    view = asyncio.run(exchange.fetch_order("0xabc"))
    assert view is not None and not view.has_record


def test_recovered_pending_intent_the_venue_never_saw_resolves_failed() -> None:
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    exchange, _ = _surviving_venue(clock)

    # The crash hit after the write-ahead PENDING checkpoint but before (or
    # inside) the send: the durable intent exists, the venue never saw it.
    store.checkpoint(_saga("0xabc", OrderState.PENDING), ts_ns=500)
    _, _, reconciler, events = _second_life(store, exchange, clock)

    assert asyncio.run(reconciler.reconcile_startup()) is True

    recovered = store.get_order("0xabc")
    assert recovered is not None
    assert recovered.state is OrderState.FAILED
    assert [type(ev) for ev in events] == [OrderFailed]
    # Attempted and proven never-landed (ADR-0010): resolved, never resent.
    view = asyncio.run(exchange.fetch_order("0xabc"))
    assert view is not None and not view.has_record


def test_recovered_live_saga_the_venue_lost_ghost_resolves_rejected_not_failed() -> None:
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    # The venue outlived our crash but lost this once-ACKed order (a paper
    # restart, an expiry, a venue-side purge): gone is not the same as un-sent.
    exchange, _ = _surviving_venue(clock)

    store.checkpoint(_saga("0xabc", OrderState.LIVE), ts_ns=500)
    _, _, reconciler, events = _second_life(store, exchange, clock)

    with capture_events() as logs:
        assert asyncio.run(reconciler.reconcile_startup()) is True

    # The ghost taxonomy applies (ADR-0010/0011 resolutions): REJECTED from
    # LIVE — FAILED would claim the send never landed, which the earlier ACK
    # disproves, and the FSM rightly forbids LIVE → FAILED.
    recovered = store.get_order("0xabc")
    assert recovered is not None
    assert recovered.state is OrderState.REJECTED
    assert not any(isinstance(ev, OrderFailed) for ev in events)
    assert "ghost.reconciled" in [log["event"] for log in logs]


def test_recovered_saga_heals_the_fill_it_missed_while_dead() -> None:
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    exchange, dead_bus = _surviving_venue(clock)

    async def first_life() -> None:
        await dead_bus.publish(_tick("42000"))
        await exchange.place(_resting_limit("0xabc"))
        # A later tick crosses the resting BUY while we are dead: the venue
        # fills it; both the LIVE ack and the fill report go unheard.
        await dead_bus.publish(_tick("40000", ts=1_500))

    asyncio.run(first_life())
    store.checkpoint(_saga("0xabc", OrderState.SUBMITTED), ts_ns=500)
    _, _, reconciler, events = _second_life(store, exchange, clock)

    assert asyncio.run(reconciler.reconcile_startup()) is True

    # No lost fill: the saga converged on the venue's executed truth — the
    # stale LIVE record must not shadow the fill history (ADR-0011 inv 4).
    recovered = store.get_order("0xabc")
    assert recovered is not None
    assert recovered.state is OrderState.FILLED
    assert recovered.cum_qty == Decimal("0.5")
    filled = [ev for ev in events if isinstance(ev, OrderFilled)]
    assert len(filled) == 1
    assert filled[0].reconciliation is True


def test_a_crash_during_recovery_just_reruns_the_pass_and_converges() -> None:
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    exchange, dead_bus = _surviving_venue(clock)

    async def first_life() -> None:
        await dead_bus.publish(_tick("42000"))
        await exchange.place(_resting_limit("0xabc"))
        await dead_bus.publish(_tick("40000", ts=1_500))

    asyncio.run(first_life())
    store.checkpoint(_saga("0xabc", OrderState.SUBMITTED), ts_ns=500)

    _, _, reconciler, _ = _second_life(store, exchange, clock)
    assert asyncio.run(reconciler.reconcile_startup()) is True

    # Death mid/after recovery: the third life rebuilds and reconciles again.
    # Dedup by trade_id/event_id makes the redo a no-op — never a double count.
    _, _, reconciler3, events3 = _second_life(store, exchange, clock)
    assert asyncio.run(reconciler3.reconcile_startup()) is True

    assert events3 == []
    recovered = store.get_order("0xabc")
    assert recovered is not None
    assert recovered.state is OrderState.FILLED
    assert recovered.cum_qty == Decimal("0.5")


class _DarkVenue(VenueDouble):
    """An ``Exchange`` behind a dead link: every read fails (``None``), and
    placing anything is a test failure — the barrier must gate all sends."""

    def __init__(self) -> None:
        self.reads = 0

    async def place(self, order: PlaceOrder) -> None:
        raise AssertionError("nothing may be placed before the barrier clears")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("nothing may be cancelled before the barrier clears")

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        self.reads += 1
        return None


def test_sustained_venue_outage_trips_the_barrier_to_faulted_after_the_window() -> None:
    clock = ManualClock(start_ns=0)
    store = SQLiteStore(":memory:")
    store.checkpoint(_saga("0xabc", OrderState.SUBMITTED), ts_ns=500)

    bus = InMemoryBus()
    checks = checkpointer(store, clock=clock)
    cache = checks.cache
    cache.rebuild()
    venue = _DarkVenue()
    reconciler = Reconciler(
        bus=bus, clock=clock, exchange=venue, cache=cache, config=ReconcileConfig()
    )

    with capture_events() as logs:
        with pytest.raises(StartupReconciliationTimeout):
            asyncio.run(_barrier(clock, reconciler).run(timeout_seconds=30.0))

    # Bounded retry with backoff: several attempts, never a tight loop.
    assert 3 <= venue.reads <= 10
    # Every frozen pass is telemetry, not silence (ADR-0020): one
    # reconcile.frozen per failed read.
    frozen = [log for log in logs if log["event"] == "reconcile.frozen"]
    assert len(frozen) == venue.reads
    assert frozen[0]["cycle"] == "startup"
    # Freeze, don't guess (ADR-0011 inv 1/5): the outage resolved nothing.
    recovered = store.get_order("0xabc")
    assert recovered is not None
    assert recovered.state is OrderState.SUBMITTED


def test_backoff_is_capped_so_faulting_never_overshoots_the_window_by_much() -> None:
    # With an uncapped doubling, a long window faults nearly a whole backoff
    # interval late (~2x the configured timeout). The cap bounds the overshoot
    # to one capped interval regardless of how long the window is.
    clock = ManualClock(start_ns=0)
    store = SQLiteStore(":memory:")
    store.checkpoint(_saga("0xabc", OrderState.SUBMITTED), ts_ns=500)

    bus = InMemoryBus()
    checks = checkpointer(store, clock=clock)
    cache = checks.cache
    cache.rebuild()
    venue = _DarkVenue()
    reconciler = Reconciler(
        bus=bus, clock=clock, exchange=venue, cache=cache, config=ReconcileConfig()
    )

    with pytest.raises(StartupReconciliationTimeout):
        asyncio.run(
            _barrier(clock, reconciler).run(timeout_seconds=300.0, max_backoff_seconds=30.0)
        )

    # Fault lands within one capped backoff of the deadline — not ~2x the window.
    faulted_at_seconds = clock.timestamp_ns() / 1_000_000_000
    assert 300.0 <= faulted_at_seconds <= 330.0


class _BlippingVenue(_DarkVenue):
    """A venue whose link comes back after a couple of failed reads."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self._failures = failures

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        self.reads += 1
        if self.reads <= self._failures:
            return None
        return VenueOrderView(status=None)  # link restored: positively no record


def test_a_transient_boot_time_blip_resolves_and_the_barrier_clears() -> None:
    clock = ManualClock(start_ns=0)
    store = SQLiteStore(":memory:")
    store.checkpoint(_saga("0xabc", OrderState.SUBMITTED), ts_ns=500)

    bus = InMemoryBus()
    checks = checkpointer(store, clock=clock)
    cache = checks.cache
    cache.rebuild()
    venue = _BlippingVenue(failures=2)
    manager = ExecutionManager(
        bus=bus,
        exchange=venue,
        checkpointer=checks,
    )
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    reconciler = Reconciler(
        bus=bus, clock=clock, exchange=venue, cache=cache, config=ReconcileConfig()
    )

    asyncio.run(_barrier(clock, reconciler).run(timeout_seconds=30.0))

    # The retry absorbed the blip; the pass then resolved the saga normally.
    assert venue.reads == 3
    recovered = store.get_order("0xabc")
    assert recovered is not None
    assert recovered.state is OrderState.FAILED


def test_on_the_paper_path_the_barrier_always_clears() -> None:
    clock = ManualClock(start_ns=2_000)
    store = SQLiteStore(":memory:")
    exchange, dead_bus = _surviving_venue(clock)

    async def first_life() -> None:
        await dead_bus.publish(_tick("42000"))
        await exchange.place(_resting_limit("0xabc"))

    asyncio.run(first_life())
    store.checkpoint(_saga("0xabc", OrderState.SUBMITTED), ts_ns=500)
    _, _, reconciler, _ = _second_life(store, exchange, clock)

    before_ns = clock.timestamp_ns()
    asyncio.run(_barrier(clock, reconciler).run(timeout_seconds=30.0))

    # In-process reads cannot fail (ADR-0024): one pass, no backoff burned.
    assert clock.timestamp_ns() == before_ns
    recovered = store.get_order("0xabc")
    assert recovered is not None
    assert recovered.state is OrderState.LIVE
