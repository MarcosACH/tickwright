"""The pre-trade guard wired through the ``ExecutionManager`` (ADR-0017/0026).

End-to-end across the real pipeline (bus → manager → paper exchange), never a
mock: a denied signal publishes ``OrderDenied`` and the exchange's ``place`` is
never reached; a tripped kill switch halts new placements while resting ``LIVE``
orders keep filling; the halt survives a restart. The same suite stays green with
``NoopGuard`` selected — the seam is real.
"""

import asyncio
from decimal import Decimal

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AggressorSide,
    ExecutionReport,
    InstrumentSpec,
    MarketTick,
    OrderDenied,
    OrderEvent,
    OrderLive,
    OrderRejected,
    OrderState,
    PlaceSignal,
    PreTradeGuard,
    Side,
    Signal,
    TimeInForce,
    derive_cloid,
)
from tickwright.domain.enums import OrderType
from tickwright.engine.cache import Cache
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.guard import NoopGuard, RealGuard
from tickwright.observability.testing import capture_events

_SPEC = InstrumentSpec(
    symbol="BTC",
    sz_decimals=3,
    max_decimals=6,
    max_sig_figs=5,
    min_notional=Decimal("10"),
)


async def _record(sink: list, event: object) -> None:
    sink.append(event)


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


def _limit_signal(price: str, *, quantity: str = "0.5", seq: int = 1) -> PlaceSignal:
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=seq,
        side=Side.BUY,
        quantity=Decimal(quantity),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal(price),
    )


def _harness(
    *, guard: PreTradeGuard | None = None
) -> tuple[InMemoryBus, ManualClock, SQLiteStore, PreTradeGuard, list[OrderEvent]]:
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    store = SQLiteStore(":memory:")
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
    cache = Cache(store=store)
    guard = guard or RealGuard(specs={"BTC": _SPEC}, store=store, clock=clock)
    manager = ExecutionManager(bus=bus, clock=clock, exchange=exchange, cache=cache, guard=guard)

    bus.subscribe(MarketTick, exchange.on_tick)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)

    order_events: list[OrderEvent] = []
    bus.subscribe(OrderEvent, lambda ev: _record(order_events, ev))
    return bus, clock, store, guard, order_events


def _market_signal(*, quantity: str, seq: int = 1) -> PlaceSignal:
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=seq,
        side=Side.BUY,
        quantity=Decimal(quantity),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def test_market_below_min_notional_is_rejected_by_the_venue_via_sourced_specs() -> None:
    # The full ADR-0031 path: the paper exchange authors the specs, the guard is
    # wired from exchange.instrument_specs() (venue-agnostic), and a too-small
    # MARKET — which the guard cannot price — is adjudicated by the venue as
    # REJECTED, the twin of the LIMIT's local DENIED.
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    store = SQLiteStore(":memory:")
    exchange = PaperExchange(
        bus=bus, clock=clock, fill_model=ImmediateFillModel(), instrument_specs={"BTC": _SPEC}
    )
    cache = Cache(store=store)
    guard = RealGuard(specs=exchange.instrument_specs(), store=store, clock=clock)
    manager = ExecutionManager(bus=bus, clock=clock, exchange=exchange, cache=cache, guard=guard)
    bus.subscribe(MarketTick, exchange.on_tick)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    order_events: list[OrderEvent] = []
    bus.subscribe(OrderEvent, lambda ev: _record(order_events, ev))
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick("100"))
        await bus.publish(_market_signal(quantity="0.05"))  # notional 5 < 10

    asyncio.run(scenario())

    # Sent (guard could not price it), then the venue refused it: REJECTED, not
    # DENIED. The saga went through the exchange, unlike the LIMIT case.
    assert any(isinstance(ev, OrderRejected) for ev in order_events)
    assert not [ev for ev in order_events if isinstance(ev, OrderDenied)]
    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.REJECTED


def test_below_min_notional_signal_is_denied_and_never_sent() -> None:
    bus, _, store, _, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        # notional = 100 × 0.05 = 5, below min_notional 10. The guard denies
        # before write-ahead: OrderDenied is the only event, and the order never
        # reaches the exchange (no OrderPlaced/Submitted/Live).
        await bus.publish(_limit_signal("100", quantity="0.05"))

    asyncio.run(scenario())

    assert [type(ev) for ev in order_events] == [OrderDenied]
    denied = order_events[0]
    assert isinstance(denied, OrderDenied)
    assert denied.reason
    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.DENIED


def test_a_denial_emits_the_order_denied_named_event() -> None:
    # A denial is a state-affecting path, so it is observable telemetry, not
    # silent (ADR-0020): order.denied carries the refusal reason, and the
    # ambient signal_id (bound for the span of signal handling) rides the record
    # rather than being repeated as a field.
    bus, _, _, _, _ = _harness()

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("100", quantity="0.05"))

    with capture_events() as logs:
        asyncio.run(scenario())

    denied = [log for log in logs if log["event"] == "order.denied"]
    assert len(denied) == 1
    assert denied[0]["signal_id"] == "trivial:BTC:1"
    assert denied[0]["reason"]


def test_noop_guard_lets_a_would_be_denied_order_through_unmodified() -> None:
    # The same below-min-notional signal the RealGuard denies passes straight
    # through NoopGuard — proving the seam is real, not hardcoded: swapping the
    # impl swaps the behavior. The order reaches the exchange and rests LIVE.
    bus, _, store, _, order_events = _harness(guard=NoopGuard())
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("41000", quantity="0.05"))

    asyncio.run(scenario())

    assert any(isinstance(ev, OrderLive) for ev in order_events)
    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.LIVE
    # Passthrough: the size is the strategy's original, never quantized.
    assert record.quantity == Decimal("0.05")


def _revived_manager(
    store: SQLiteStore,
) -> tuple[InMemoryBus, RealGuard, list[OrderEvent]]:
    """A fresh engine over a surviving store — the restart the barrier gates."""
    bus = InMemoryBus()
    clock = ManualClock(start_ns=2_000)
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
    cache = Cache(store=store)
    cache.rebuild()
    guard = RealGuard(specs={"BTC": _SPEC}, store=store, clock=clock)
    manager = ExecutionManager(bus=bus, clock=clock, exchange=exchange, cache=cache, guard=guard)
    bus.subscribe(MarketTick, exchange.on_tick)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    events: list[OrderEvent] = []
    bus.subscribe(OrderEvent, lambda ev: _record(events, ev))
    return bus, guard, events


def test_kill_switch_survives_restart_and_reset_re_enables_placement() -> None:
    # First life: halt the engine, then crash. Only the store survives.
    _, _, store, guard, _ = _harness()
    guard.trip_kill_switch("halt before crash")

    # Second life: the revived guard restores the sticky halt before trading, so
    # a placement is still DENIED — a crash never silently un-halts (ADR-0026).
    bus2, guard2, events2 = _revived_manager(store)
    assert guard2.kill_switch_tripped

    async def still_halted() -> None:
        await bus2.publish(_tick("42000"))
        await bus2.publish(_limit_signal("41000", seq=1))

    asyncio.run(still_halted())
    assert [type(ev) for ev in events2] == [OrderDenied]

    # Only an explicit reset lifts it; then placement works again.
    guard2.reset_kill_switch()

    async def placement_restored() -> None:
        await bus2.publish(_limit_signal("41000", seq=2))

    asyncio.run(placement_restored())
    assert any(isinstance(ev, OrderLive) for ev in events2)


def test_kill_switch_denies_new_orders_while_resting_orders_keep_filling() -> None:
    bus, _, store, guard, order_events = _harness()
    resting = derive_cloid("trivial:BTC:1")
    halted = derive_cloid("trivial:BTC:2")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("41000", seq=1))  # rests LIVE below market

        guard.trip_kill_switch("operator halt")

        # A new placement while halted is DENIED, never sent (ADR-0026).
        await bus.publish(_limit_signal("40000", seq=2))
        # The resting LIVE order is untouched by the halt — a later crossing tick
        # still fills it (halt-only, never auto-cancels).
        await bus.publish(_tick("41000"))

    asyncio.run(scenario())

    assert any(isinstance(ev, OrderDenied) and ev.cloid == halted for ev in order_events)
    assert store.get_order(halted).state is OrderState.DENIED  # type: ignore[union-attr]
    # The resting order rode through the halt: it went LIVE and then FILLED.
    assert any(isinstance(ev, OrderLive) and ev.cloid == resting for ev in order_events)
    assert store.get_order(resting).state is OrderState.FILLED  # type: ignore[union-attr]
