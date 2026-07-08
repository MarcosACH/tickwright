"""The catalog-walking contract: every named event has a path, every path a test.

"A state-affecting path with no named event is a defect" (ADR-0020) is only
enforceable if the catalog is walkable and each name is proven to be emitted by
a test that drives its real path. This module *is* that walk: one scenario per
``NamedEvent``, each exercising the shipped path end-to-end (no mocks of our own
classes), plus a coverage assertion that the walk names every catalog member —
so a future slice that adds a name without a path-and-test fails here.

The scenarios deliberately re-drive the paths in miniature rather than lean on
the layer suites, so the walk reads as a single, self-contained census of the
engine's observable surface.
"""

import asyncio
from collections.abc import Callable
from decimal import Decimal

import pytest

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AggressorSide,
    ExecutionReport,
    FillReport,
    InstrumentSpec,
    MarketTick,
    Order,
    OrderEvent,
    OrderState,
    OrderStatusReport,
    PlaceSignal,
    Side,
    Signal,
    VenueOrderView,
    derive_cloid,
)
from tickwright.domain.enums import OrderType, TimeInForce
from tickwright.engine.cache import Cache
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.guard import RealGuard
from tickwright.engine.reconcile import ReconcileConfig, Reconciler
from tickwright.engine.runner import Engine
from tickwright.engine.strategy_host import StrategyHost
from tickwright.observability import NamedEvent
from tickwright.observability.testing import capture_events

_SPEC = InstrumentSpec(
    symbol="BTC", sz_decimals=3, max_decimals=6, max_sig_figs=5, min_notional=Decimal("10")
)


# --- Builders ----------------------------------------------------------------


def _tick(price: str = "42000") -> MarketTick:
    return MarketTick(
        ts_event=1_000,
        ts_init=1_000,
        symbol="BTC",
        price=Decimal(price),
        size=Decimal("10"),
        aggressor_side=AggressorSide.BUY,
        trade_id="t1",
        seq=0,
    )


def _market_signal(*, quantity: str = "0.5") -> PlaceSignal:
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=1,
        side=Side.BUY,
        quantity=Decimal(quantity),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def _limit_signal(*, price: str, quantity: str = "0.5") -> PlaceSignal:
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=1,
        side=Side.BUY,
        quantity=Decimal(quantity),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal(price),
    )


def _saga(state: OrderState, *, cloid: str = "0xabc") -> Order:
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


def _status(status: OrderState, *, cloid: str = "0xabc") -> OrderStatusReport:
    return OrderStatusReport(
        ts_event=2_000, ts_init=2_000, cloid=cloid, symbol="BTC", status=status
    )


class _SilentExchange:
    """A venue that accepts a placement and says nothing back — an order rests
    ``SUBMITTED`` so a synthetic status/fill can drive the next transition."""

    async def place(self, order: object) -> None:
        return None

    async def cancel(self, cloid: str) -> None:
        return None

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        return None


class _ForgetfulVenue:
    """A venue that answers positively but has lost this order (``status=None``)."""

    async def place(self, order: object) -> None:
        raise AssertionError("the reconcile walk never places")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("the reconcile walk never cancels")

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        return VenueOrderView(status=None)


class _DarkVenue:
    """A venue whose reads all fail (``None``) — the connectivity guard trips."""

    async def place(self, order: object) -> None:
        raise AssertionError("the frozen cycle never places")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("the frozen cycle never cancels")

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        return None


class _Strategy:
    """A minimal strategy whose handlers raise, to exercise containment."""

    def __init__(self, strategy_id: str = "trivial") -> None:
        self.strategy_id = strategy_id

    async def on_tick(self, tick: MarketTick) -> None:
        raise KeyError("third-party bug")

    async def on_order_event(self, event: OrderEvent) -> None:
        return None

    def set_next_seq(self, next_seq: int) -> None:
        return None

    def snapshot(self) -> bytes:
        return b""

    def restore(self, data: bytes) -> None:
        raise ValueError("unknown snapshot version")


# --- Wiring ------------------------------------------------------------------


def _manager(
    bus: InMemoryBus, clock: ManualClock, exchange: object, *, guard: object | None = None
) -> None:
    """Wire an ``ExecutionManager`` over ``exchange`` onto ``bus`` — the exchange
    must already share ``bus`` so its fills flow back to the manager."""
    cache = Cache(store=SQLiteStore(":memory:"))
    manager = ExecutionManager(
        bus=bus,
        clock=clock,
        exchange=exchange,  # type: ignore[arg-type]
        cache=cache,
        guard=guard,  # type: ignore[arg-type]
    )
    if isinstance(exchange, PaperExchange):
        bus.subscribe(MarketTick, exchange.on_tick)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)


def _reconciler(state: OrderState, venue: object, config: ReconcileConfig) -> Reconciler:
    clock = ManualClock(start_ns=0)
    store = SQLiteStore(":memory:")
    store.checkpoint(_saga(state), ts_ns=500)
    cache = Cache(store=store)
    cache.rebuild()
    return Reconciler(
        bus=InMemoryBus(),
        clock=clock,
        exchange=venue,  # type: ignore[arg-type]
        cache=cache,
        config=config,
    )


# --- Scenarios: each drives one path that must emit its named event ----------


def _drive_market_fill() -> None:
    """MARKET order through the paper exchange: placed → submitted → filled."""
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
    _manager(bus, clock, exchange)

    async def go() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_market_signal())

    asyncio.run(go())


def _drive_partial_fill() -> None:
    bus = InMemoryBus()
    _manager(bus, ManualClock(start_ns=1_000), _SilentExchange())
    cloid = derive_cloid("trivial:BTC:1")

    async def go() -> None:
        await bus.publish(_market_signal(quantity="1.0"))  # rests SUBMITTED
        await bus.publish(
            FillReport(
                ts_event=2_000,
                ts_init=2_000,
                cloid=cloid,
                symbol="BTC",
                trade_id="f1",
                quantity=Decimal("0.4"),
                price=Decimal("42000"),
            )
        )

    asyncio.run(go())


def _drive_denied() -> None:
    store = SQLiteStore(":memory:")
    guard = RealGuard(specs={"BTC": _SPEC}, store=store, clock=ManualClock(start_ns=1_000))
    bus = InMemoryBus()
    _manager(bus, ManualClock(start_ns=1_000), _SilentExchange(), guard=guard)

    async def go() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal(price="100", quantity="0.05"))  # below min notional

    asyncio.run(go())


def _drive_status(*statuses: OrderState) -> Callable[[], None]:
    def scenario() -> None:
        bus = InMemoryBus()
        _manager(bus, ManualClock(start_ns=1_000), _SilentExchange())
        cloid = derive_cloid("trivial:BTC:1")

        async def go() -> None:
            await bus.publish(_market_signal())  # rests SUBMITTED
            for status in statuses:
                await bus.publish(_status(status, cloid=cloid))

        asyncio.run(go())

    return scenario


def _drive_kill_switch(*, reset: bool) -> Callable[[], None]:
    def scenario() -> None:
        guard = RealGuard(specs={"BTC": _SPEC}, store=SQLiteStore(":memory:"), clock=ManualClock())
        guard.trip_kill_switch("operator halt")
        if reset:
            guard.reset_kill_switch()

    return scenario


def _drive_strategy_error() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(), store=SQLiteStore(":memory:"))
    host.register(_Strategy(), symbols={"BTC"})
    host.start()

    async def go() -> None:
        await bus.publish(_tick("42000"))

    asyncio.run(go())


def _drive_snapshot_incompatible() -> None:
    store = SQLiteStore(":memory:")
    store.save_strategy_snapshot("trivial", b"old-shape", ts_ns=1_000)
    host = StrategyHost(bus=InMemoryBus(), clock=ManualClock(), store=store)
    host.register(_Strategy(), symbols={"BTC"})
    host.start()  # restore() raises → strategy.snapshot_incompatible


def _drive_inflight_reconciled() -> None:
    config = ReconcileConfig(inflight_interval_seconds=5.0, inflight_max_attempts=1)
    reconciler = _reconciler(OrderState.SUBMITTED, _ForgetfulVenue(), config)
    assert asyncio.run(reconciler.reconcile_inflight()) is True


def _drive_recency_skipped() -> None:
    clock = ManualClock(start_ns=1_000_000_000)
    store = SQLiteStore(":memory:")
    cache = Cache(store=store)
    # An in-session checkpoint makes the saga's recency known and fresh, so an
    # absent venue read is skipped inside the protection window (ADR-0011 inv 3).
    cache.checkpoint(_saga(OrderState.LIVE), ts_ns=clock.timestamp_ns())
    reconciler = Reconciler(
        bus=InMemoryBus(),
        clock=clock,
        exchange=_ForgetfulVenue(),  # type: ignore[arg-type]
        cache=cache,
        config=ReconcileConfig(recent_order_protection_seconds=30.0, ghost_grace_seconds=90.0),
    )
    assert asyncio.run(reconciler.reconcile_open_orders()) is True


def _drive_ghost_reconciled() -> None:
    clock = ManualClock(start_ns=0)
    store = SQLiteStore(":memory:")
    store.checkpoint(_saga(OrderState.LIVE), ts_ns=500)
    cache = Cache(store=store)
    cache.rebuild()  # store-recovered: recency unknown, only the grace window guards
    reconciler = Reconciler(
        bus=InMemoryBus(),
        clock=clock,
        exchange=_ForgetfulVenue(),  # type: ignore[arg-type]
        cache=cache,
        config=ReconcileConfig(ghost_grace_seconds=90.0),
    )

    async def go() -> None:
        assert await reconciler.reconcile_open_orders() is True  # arms the grace clock
        await clock.sleep(91.0)
        assert await reconciler.reconcile_open_orders() is True  # continuous absence → ghost

    asyncio.run(go())


def _drive_frozen() -> None:
    reconciler = _reconciler(OrderState.SUBMITTED, _DarkVenue(), ReconcileConfig())
    assert asyncio.run(reconciler.reconcile_inflight()) is False


class _IdleFeed:
    """A feed with nothing to say — the lifecycle walk needs the ordered
    startup, not ticks. A ``MarketFeed`` double at the venue boundary."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _drive_engine_lifecycle() -> None:
    """One supervised startup-and-stop: barrier cleared, feed started (ADR-0024)."""

    async def go() -> None:
        bus = InMemoryBus()
        clock = ManualClock()
        engine = Engine(
            bus=bus,
            clock=clock,
            store=SQLiteStore(":memory:"),
            exchange=PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel()),
            feed=_IdleFeed(),
        )
        run = asyncio.create_task(engine.run())
        await engine.stop()
        assert await run == 0

    asyncio.run(go())


# --- The catalog walk --------------------------------------------------------

# Every NamedEvent → a scenario that drives its real path. Several of the saga
# events fall out of one lifecycle scenario; a status scenario is parametrized by
# the transition it feeds. A name missing from this map fails the coverage test.
SCENARIOS: dict[NamedEvent, Callable[[], None]] = {
    NamedEvent.ORDER_PLACED: _drive_market_fill,
    NamedEvent.ORDER_SUBMITTED: _drive_market_fill,
    NamedEvent.ORDER_FILLED: _drive_market_fill,
    NamedEvent.ORDER_PARTIALLY_FILLED: _drive_partial_fill,
    NamedEvent.ORDER_DENIED: _drive_denied,
    NamedEvent.ORDER_LIVE: _drive_status(OrderState.LIVE),
    NamedEvent.ORDER_REJECTED: _drive_status(OrderState.REJECTED),
    NamedEvent.ORDER_FAILED: _drive_status(OrderState.FAILED),
    NamedEvent.ORDER_CANCELLED: _drive_status(OrderState.LIVE, OrderState.CANCELLED),
    NamedEvent.ENGINE_BARRIER_CLEARED: _drive_engine_lifecycle,
    NamedEvent.ENGINE_FEED_STARTED: _drive_engine_lifecycle,
    NamedEvent.GUARD_KILL_SWITCH_TRIPPED: _drive_kill_switch(reset=False),
    NamedEvent.GUARD_KILL_SWITCH_RESET: _drive_kill_switch(reset=True),
    NamedEvent.STRATEGY_ERROR: _drive_strategy_error,
    NamedEvent.STRATEGY_SNAPSHOT_INCOMPATIBLE: _drive_snapshot_incompatible,
    NamedEvent.INFLIGHT_RECONCILED: _drive_inflight_reconciled,
    NamedEvent.RECONCILE_RECENCY_SKIPPED: _drive_recency_skipped,
    NamedEvent.GHOST_RECONCILED: _drive_ghost_reconciled,
    NamedEvent.RECONCILE_FROZEN: _drive_frozen,
}


@pytest.mark.parametrize("event", list(NamedEvent), ids=lambda e: e.value)
def test_every_cataloged_event_is_emitted_by_its_path(event: NamedEvent) -> None:
    with capture_events() as logs:
        SCENARIOS[event]()

    emitted = {log["event"] for log in logs}
    assert event.value in emitted, f"{event.value} was not emitted by its scenario"


def test_the_walk_names_every_catalog_member() -> None:
    # The contract's teeth: a new NamedEvent with no scenario (hence no path and
    # no test) fails here — "a state-affecting path with no named event is a
    # defect", made executable (ADR-0020).
    assert set(SCENARIOS) == set(NamedEvent)
