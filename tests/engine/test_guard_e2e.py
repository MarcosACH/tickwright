"""The pre-trade guard wired through the ``ExecutionManager`` (ADR-0017/0026).

End-to-end across the real pipeline (bus → manager → paper exchange), never a
mock: a denied signal publishes ``OrderDenied`` and the exchange's ``place`` is
never reached; a tripped kill switch halts new placements while resting ``LIVE``
orders keep filling; the halt survives a restart. The same suite stays green with
``NoopGuard`` selected — the seam is real.
"""

import asyncio
import random
from decimal import Decimal

from ledgers import GENESIS, checkpointer

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import (
    FillModel,
    ImmediateFillModel,
    PaperExchange,
    StochasticFillModel,
    StochasticParams,
)
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
    ReconciliationFill,
    Side,
    Signal,
    TimeInForce,
    derive_cloid,
)
from tickwright.domain.enums import OrderType
from tickwright.engine.checkpoint import Checkpointer
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.guard import (
    NO_LIMITS,
    NoopGuard,
    PreTradeLimits,
    RealGuard,
    SymbolLimits,
)
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
    *,
    guard: PreTradeGuard | None = None,
    limits: PreTradeLimits = NO_LIMITS,
    partial_fill_fraction: str | None = None,
) -> tuple[InMemoryBus, Checkpointer, SQLiteStore, PreTradeGuard, list[OrderEvent]]:
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    store = SQLiteStore(":memory:")
    fill_model: FillModel = ImmediateFillModel()
    if partial_fill_fraction is not None:
        # With its other knobs inert, the seeded model fills like the immediate
        # one, except that a crossing limit fills only this fraction per tick.
        params = StochasticParams(partial_fill_fraction=Decimal(partial_fill_fraction))
        fill_model = StochasticFillModel(rng=random.Random(0), clock=clock, params=params)
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=fill_model,
        genesis_collateral=GENESIS,
        account_net=dict,
    )
    checks = checkpointer(store, clock=clock)
    guard = guard or RealGuard(specs={"BTC": _SPEC}, store=store, clock=clock, limits=limits)
    manager = ExecutionManager(
        bus=bus,
        exchange=exchange,
        checkpointer=checks,
        guard=guard,
    )

    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)

    order_events: list[OrderEvent] = []
    bus.subscribe(OrderEvent, lambda ev: _record(order_events, ev))
    return bus, checks, store, guard, order_events


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
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        instrument_specs={"BTC": _SPEC},
        account_net=dict,
    )
    checks = checkpointer(store, clock=clock)
    guard = RealGuard(specs=exchange.instrument_specs(), store=store, clock=clock)
    manager = ExecutionManager(
        bus=bus,
        exchange=exchange,
        checkpointer=checks,
        guard=guard,
    )
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


def test_an_order_above_the_max_order_size_is_never_sent_and_a_smaller_one_passes() -> None:
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_size=Decimal("0.2"))})
    bus, _, store, _, order_events = _harness(limits=limits)
    too_big = derive_cloid("trivial:BTC:1")
    fits = derive_cloid("trivial:BTC:2")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        # Both rest below the market, so the second one ends LIVE, not FILLED.
        await bus.publish(_limit_signal("100", quantity="0.3", seq=1))
        await bus.publish(_limit_signal("100", quantity="0.2", seq=2))

    asyncio.run(scenario())

    assert [type(ev) for ev in order_events if ev.cloid == too_big] == [OrderDenied]
    denied = store.get_order(too_big)
    assert denied is not None
    assert denied.state is OrderState.DENIED
    passed = store.get_order(fits)
    assert passed is not None
    assert passed.state is OrderState.LIVE


def test_the_max_position_counts_open_buys_by_their_unfilled_remainder() -> None:
    # Cap 1. The first buy of 0.5 fills 0.2 and rests 0.3, so the worst case is
    # +0.5 before the next order. A buy of 0.5 lands exactly on the cap. A buy
    # of 0.1 after it would reach 1.1, so it is denied.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_position=Decimal("1"))})
    bus, _, store, _, _ = _harness(limits=limits, partial_fill_fraction="0.4")
    partly_filled = derive_cloid("trivial:BTC:1")
    at_cap = derive_cloid("trivial:BTC:2")
    past_cap = derive_cloid("trivial:BTC:3")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("41000", quantity="0.5", seq=1))
        await bus.publish(_tick("41000"))  # crosses the first buy: one partial fill
        # Both rest below the market, so neither fills.
        await bus.publish(_limit_signal("40000", quantity="0.5", seq=2))
        await bus.publish(_limit_signal("40000", quantity="0.1", seq=3))

    asyncio.run(scenario())

    first = store.get_order(partly_filled)
    assert first is not None
    assert (first.state, first.cum_qty) == (OrderState.PARTIALLY_FILLED, Decimal("0.2"))
    assert store.get_order(at_cap).state is OrderState.LIVE  # type: ignore[union-attr]
    assert store.get_order(past_cap).state is OrderState.DENIED  # type: ignore[union-attr]


def test_the_max_position_counts_size_placed_by_hand() -> None:
    # Cap 1. A user bought 0.8 by hand on the venue. Paper holds no position of
    # its own, so the size arrives the one way it can: the reconciler books it
    # as a heal into the unattributed partition. A buy of 0.2 lands exactly on
    # the cap. A buy of 0.1 after it would reach 1.1, so it is denied.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_position=Decimal("1"))})
    bus, checks, store, _, _ = _harness(limits=limits)
    by_hand = ReconciliationFill(
        symbol="BTC", side=Side.BUY, quantity=Decimal("0.8"), price=Decimal("42000"), ts_ns=1_000
    )
    checks.checkpoint_heal((by_hand,))
    at_cap = derive_cloid("trivial:BTC:1")
    past_cap = derive_cloid("trivial:BTC:2")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        # Both rest below the market, so neither fills.
        await bus.publish(_limit_signal("40000", quantity="0.2", seq=1))
        await bus.publish(_limit_signal("40000", quantity="0.1", seq=2))

    asyncio.run(scenario())

    assert store.get_order(at_cap).state is OrderState.LIVE  # type: ignore[union-attr]
    assert store.get_order(past_cap).state is OrderState.DENIED  # type: ignore[union-attr]


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
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
    )
    checks = checkpointer(store, clock=clock)
    cache = checks.cache
    cache.rebuild()
    guard = RealGuard(specs={"BTC": _SPEC}, store=store, clock=clock)
    manager = ExecutionManager(
        bus=bus,
        exchange=exchange,
        checkpointer=checks,
        guard=guard,
    )
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
