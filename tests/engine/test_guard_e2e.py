"""The pre-trade guard wired through the ``ExecutionManager`` (ADR-0017/0026).

End-to-end across the real pipeline (bus → manager → paper exchange), never a
mock: a denied signal publishes ``OrderDenied`` and the exchange's ``place`` is
never reached; a tripped kill switch halts new placements while resting ``LIVE``
orders keep filling; the halt survives a restart. The same suite stays green with
``NoopGuard`` selected — the seam is real.
"""

import asyncio
import random
from dataclasses import dataclass
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
    MarkTick,
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
_ETH_SPEC = InstrumentSpec(
    symbol="ETH",
    sz_decimals=4,
    max_decimals=6,
    max_sig_figs=5,
    min_notional=Decimal("10"),
)


async def _record(sink: list, event: object) -> None:
    sink.append(event)


def _tick(price: str = "42000", *, symbol: str = "BTC") -> MarketTick:
    return MarketTick(
        ts_event=1_000,
        ts_init=1_000,
        symbol=symbol,
        price=Decimal(price),
        size=Decimal("10"),
        aggressor_side=AggressorSide.BUY,
        trade_id="t1",
        seq=0,
    )


def _mark(price: str, *, ts_ns: int = 1_000) -> MarkTick:
    return MarkTick(ts_event=ts_ns, ts_init=ts_ns, symbol="BTC", price=Decimal(price))


def _limit_signal(
    price: str,
    *,
    quantity: str = "0.5",
    seq: int = 1,
    side: Side = Side.BUY,
    symbol: str = "BTC",
) -> PlaceSignal:
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol=symbol,
        seq=seq,
        side=side,
        quantity=Decimal(quantity),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal(price),
    )


@dataclass(frozen=True, slots=True)
class _Engine:
    bus: InMemoryBus
    clock: ManualClock
    store: SQLiteStore
    checks: Checkpointer
    guard: PreTradeGuard
    events: list[OrderEvent]


def _engine(
    *,
    store: SQLiteStore | None = None,
    guard: PreTradeGuard | None = None,
    limits: PreTradeLimits = NO_LIMITS,
    partial_fill_fraction: str | None = None,
    start_ns: int = 1_000,
) -> _Engine:
    """One engine life, booted in the runner's order. Pass a surviving store for
    a restart, so the second life is built the same way as the first."""
    bus = InMemoryBus()
    clock = ManualClock(start_ns=start_ns)
    store = store or SQLiteStore(":memory:")
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
    # The runner's boot step: the ledger first, then the order cache. Rebuilding
    # the cache alone would bring the open orders back without the position.
    checks.recover()
    specs = {"BTC": _SPEC, "ETH": _ETH_SPEC}
    guard = guard or RealGuard(specs=specs, store=store, clock=clock, limits=limits)
    manager = ExecutionManager(
        bus=bus,
        exchange=exchange,
        checkpointer=checks,
        guard=guard,
    )

    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)

    # The runner hands each mark to the projection, which lends it to the
    # guard's reading. Without this a mark published here would reach nobody.
    async def observe_mark(mark: MarkTick) -> None:
        checks.portfolio.observe_mark(mark)

    bus.subscribe(MarkTick, observe_mark)

    order_events: list[OrderEvent] = []
    bus.subscribe(OrderEvent, lambda ev: _record(order_events, ev))
    return _Engine(bus, clock, store, checks, guard, order_events)


def _market_signal(*, quantity: str, seq: int = 1, side: Side = Side.BUY) -> PlaceSignal:
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=seq,
        side=side,
        quantity=Decimal(quantity),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def _state(store: SQLiteStore, cloid: str) -> OrderState:
    order = store.get_order(cloid)
    assert order is not None, f"no order {cloid} in the store"
    return order.state


def _assert_denied_by_max_position(
    store: SQLiteStore, order_events: list[OrderEvent], cloid: str
) -> None:
    """Assert the max position denied the order before it reached the exchange."""
    # With OrderDenied as its only event, the order never got an OrderPlaced.
    events = [ev for ev in order_events if ev.cloid == cloid]
    assert [type(ev) for ev in events] == [OrderDenied]
    denied = events[0]
    assert isinstance(denied, OrderDenied)
    assert "max position" in denied.reason
    assert _state(store, cloid) is OrderState.DENIED


def _assert_denied_by(engine: _Engine, cloid: str, reason: str) -> None:
    """Assert the guard denied the order with ``reason``, before the exchange."""
    # With OrderDenied as its only event, the order never got an OrderPlaced.
    events = [ev for ev in engine.events if ev.cloid == cloid]
    assert [type(ev) for ev in events] == [OrderDenied]
    denied = events[0]
    assert isinstance(denied, OrderDenied)
    assert denied.reason == reason
    assert _state(engine.store, cloid) is OrderState.DENIED


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
    engine = _engine()
    bus, store, order_events = engine.bus, engine.store, engine.events
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
    engine = _engine(limits=limits)
    bus, store, order_events = engine.bus, engine.store, engine.events
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


def test_a_limit_order_worth_more_than_the_max_order_value_is_never_sent() -> None:
    # Cap 1000 USD. At 40000, a buy of 0.03 is worth 1200 and a buy of 0.02 is
    # worth 800.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_value=Decimal("1000"))})
    engine = _engine(limits=limits)
    too_big = derive_cloid("trivial:BTC:1")
    fits = derive_cloid("trivial:BTC:2")

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        # Both rest below the market, so the second one ends LIVE, not FILLED.
        await engine.bus.publish(_limit_signal("40000", quantity="0.03", seq=1))
        await engine.bus.publish(_limit_signal("40000", quantity="0.02", seq=2))

    asyncio.run(scenario())

    _assert_denied_by(engine, too_big, "above max order value 1000")
    assert _state(engine.store, fits) is OrderState.LIVE


def test_a_market_order_is_valued_at_the_mark_against_the_max_order_value() -> None:
    # Cap 1000 USD, mark 40000. A buy of 0.03 is worth 1200 and a buy of 0.02
    # is worth 800. The last trade at 42000 would value 0.02 at 840, still
    # under the cap, so the pass does not depend on which price is used.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_value=Decimal("1000"))})
    engine = _engine(limits=limits)
    too_big = derive_cloid("trivial:BTC:1")
    fits = derive_cloid("trivial:BTC:2")

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        await engine.bus.publish(_mark("40000"))
        await engine.bus.publish(_market_signal(quantity="0.03", seq=1))
        await engine.bus.publish(_market_signal(quantity="0.02", seq=2))

    asyncio.run(scenario())

    _assert_denied_by(engine, too_big, "above max order value 1000")
    assert _state(engine.store, fits) is OrderState.FILLED


def test_a_sell_limit_below_the_mark_is_valued_at_the_mark() -> None:
    # Cap 1000 USD, mark 40000. A sell of 0.03 at a limit of 1000 is worth 30 at
    # its own price, but a real venue fills it near the bid, about 1200 (#391).
    # A sell of 0.02 is worth 800 at the mark, so it still passes.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_value=Decimal("1000"))})
    engine = _engine(limits=limits)
    too_big = derive_cloid("trivial:BTC:1")
    fits = derive_cloid("trivial:BTC:2")

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        await engine.bus.publish(_mark("40000"))
        await engine.bus.publish(_limit_signal("1000", quantity="0.03", seq=1, side=Side.SELL))
        await engine.bus.publish(_limit_signal("1000", quantity="0.02", seq=2, side=Side.SELL))

    asyncio.run(scenario())

    _assert_denied_by(engine, too_big, "above max order value 1000")
    assert _state(engine.store, fits) is OrderState.FILLED


def test_a_sell_limit_above_the_mark_is_valued_at_its_limit_price() -> None:
    # Cap 1000 USD, mark 40000. A sell of 0.02 at a limit of 60000 is worth 1200
    # at its own price and 800 at the mark. It can only fill at its limit or
    # better, so its limit price is the value. A sell of 0.01 is worth 600.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_value=Decimal("1000"))})
    engine = _engine(limits=limits)
    too_big = derive_cloid("trivial:BTC:1")
    fits = derive_cloid("trivial:BTC:2")

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        await engine.bus.publish(_mark("40000"))
        # Both rest above the market, so the second one ends LIVE, not FILLED.
        await engine.bus.publish(_limit_signal("60000", quantity="0.02", seq=1, side=Side.SELL))
        await engine.bus.publish(_limit_signal("60000", quantity="0.01", seq=2, side=Side.SELL))

    asyncio.run(scenario())

    _assert_denied_by(engine, too_big, "above max order value 1000")
    assert _state(engine.store, fits) is OrderState.LIVE


def test_a_sell_limit_with_no_mark_is_denied_when_a_max_order_value_is_set() -> None:
    # Without a mark the guard cannot prove a sell limit fits, because it may
    # fill far above its own price (#391). A buy limit needs no mark.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_value=Decimal("1000"))})
    engine = _engine(limits=limits)
    sell = derive_cloid("trivial:BTC:1")
    buy = derive_cloid("trivial:BTC:2")

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))  # a trade, but no mark yet
        await engine.bus.publish(_limit_signal("60000", quantity="0.001", seq=1, side=Side.SELL))
        await engine.bus.publish(_limit_signal("40000", quantity="0.001", seq=2, side=Side.BUY))

    asyncio.run(scenario())

    _assert_denied_by(engine, sell, "no mark for max order value 1000")
    assert _state(engine.store, buy) is OrderState.LIVE


def test_a_stale_mark_denies_a_sell_limit_until_a_fresh_mark_arrives() -> None:
    # Cap 1000 and the default mark max age of 10 seconds. Every sell is worth
    # 60 at its limit price, far under the cap, so only the mark's age can deny it.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_value=Decimal("1000"))})
    engine = _engine(limits=limits, start_ns=1_000)
    ten_seconds = 10_000_000_000

    def sell(seq: int) -> PlaceSignal:
        # Above the market, so it rests LIVE when it passes.
        return _limit_signal("60000", quantity="0.001", seq=seq, side=Side.SELL)

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        await engine.bus.publish(_mark("40000", ts_ns=1_000))
        engine.clock.advance_to(1_000 + ten_seconds)  # exactly the max age
        await engine.bus.publish(sell(1))
        engine.clock.advance_to(1_000 + ten_seconds + 1)  # one nanosecond past it
        await engine.bus.publish(sell(2))
        fresh = engine.clock.timestamp_ns()
        await engine.bus.publish(_mark("40000", ts_ns=fresh))
        await engine.bus.publish(sell(3))

    asyncio.run(scenario())

    assert _state(engine.store, derive_cloid("trivial:BTC:1")) is OrderState.LIVE
    _assert_denied_by(engine, derive_cloid("trivial:BTC:2"), "stale mark for max order value 1000")
    assert _state(engine.store, derive_cloid("trivial:BTC:3")) is OrderState.LIVE


def test_a_market_order_with_no_mark_is_denied_when_a_max_order_value_is_set() -> None:
    # The guard cannot prove the order fits the cap, so it refuses (ADR-0051).
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_value=Decimal("1000"))})
    engine = _engine(limits=limits)
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))  # a trade, but no mark yet
        await engine.bus.publish(_market_signal(quantity="0.001"))

    asyncio.run(scenario())

    _assert_denied_by(engine, cloid, "no mark for max order value 1000")


def test_a_market_order_with_no_mark_passes_when_no_max_order_value_is_set() -> None:
    # A symbol with other caps but no value cap never needs a mark.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_size=Decimal("1"))})
    engine = _engine(limits=limits)

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        await engine.bus.publish(_market_signal(quantity="0.001"))

    asyncio.run(scenario())

    assert _state(engine.store, derive_cloid("trivial:BTC:1")) is OrderState.FILLED


def test_a_stale_mark_denies_a_market_order_until_a_fresh_mark_arrives() -> None:
    # Cap 1000 and the default mark max age of 10 seconds. Every order is worth
    # 40 at the mark, far under the cap, so only the mark's age can deny it.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_value=Decimal("1000"))})
    engine = _engine(limits=limits, start_ns=1_000)
    ten_seconds = 10_000_000_000

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        await engine.bus.publish(_mark("40000", ts_ns=1_000))
        engine.clock.advance_to(1_000 + ten_seconds)  # exactly the max age
        await engine.bus.publish(_market_signal(quantity="0.001", seq=1))
        engine.clock.advance_to(1_000 + ten_seconds + 1)  # one nanosecond past it
        await engine.bus.publish(_market_signal(quantity="0.001", seq=2))
        fresh = engine.clock.timestamp_ns()
        await engine.bus.publish(_mark("40000", ts_ns=fresh))
        await engine.bus.publish(_market_signal(quantity="0.001", seq=3))

    asyncio.run(scenario())

    assert _state(engine.store, derive_cloid("trivial:BTC:1")) is OrderState.FILLED
    _assert_denied_by(engine, derive_cloid("trivial:BTC:2"), "stale mark for max order value 1000")
    assert _state(engine.store, derive_cloid("trivial:BTC:3")) is OrderState.FILLED


def test_the_max_position_counts_open_buys_by_their_unfilled_remainder() -> None:
    # Cap 1. The first buy of 0.5 fills 0.2 and rests 0.3, so the worst case is
    # +0.5 before the next order. A buy of 0.5 lands exactly on the cap. A buy
    # of 0.1 after it would reach 1.1, so it is denied.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_position=Decimal("1"))})
    engine = _engine(limits=limits, partial_fill_fraction="0.4")
    bus, store, order_events = engine.bus, engine.store, engine.events
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
    assert _state(store, at_cap) is OrderState.LIVE
    _assert_denied_by_max_position(store, order_events, past_cap)


def test_the_max_position_counts_size_placed_by_hand() -> None:
    # Cap 1. A user bought 0.8 by hand on the venue. Paper holds no position of
    # its own, so the size arrives the one way it can: the reconciler books it
    # as a heal into the unattributed partition. A buy of 0.2 lands exactly on
    # the cap. A buy of 0.1 after it would reach 1.1, so it is denied.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_position=Decimal("1"))})
    engine = _engine(limits=limits)
    bus, checks, store, order_events = engine.bus, engine.checks, engine.store, engine.events
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

    assert _state(store, at_cap) is OrderState.LIVE
    _assert_denied_by_max_position(store, order_events, past_cap)


def test_a_denial_emits_the_order_denied_named_event() -> None:
    # A denial is a state-affecting path, so it is observable telemetry, not
    # silent (ADR-0020): order.denied carries the refusal reason, and the
    # ambient signal_id (bound for the span of signal handling) rides the record
    # rather than being repeated as a field.
    bus = _engine().bus

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
    engine = _engine(guard=NoopGuard())
    bus, store, order_events = engine.bus, engine.store, engine.events
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


def test_kill_switch_survives_restart_and_reset_re_enables_placement() -> None:
    # First life: halt the engine, then crash. Only the store survives.
    first = _engine()
    first.guard.trip_kill_switch("halt before crash")

    # Second life: the revived guard restores the sticky halt before trading, so
    # a placement is still DENIED — a crash never silently un-halts (ADR-0026).
    second = _engine(store=first.store, start_ns=2_000)
    bus2, guard2, events2 = second.bus, second.guard, second.events
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


def test_after_a_restart_the_max_position_denies_the_same_order_again() -> None:
    # First life, cap 1: a buy of 0.5 fills 0.2 and rests 0.3, and a buy of 0.5
    # rests. The worst case is +1, so a further buy of 0.1 is denied.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_position=Decimal("1"))})
    engine = _engine(limits=limits, partial_fill_fraction="0.4")
    bus, store, order_events = engine.bus, engine.store, engine.events

    async def first_life() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("41000", quantity="0.5", seq=1))
        await bus.publish(_tick("41000"))  # crosses the first buy: one partial fill
        await bus.publish(_limit_signal("40000", quantity="0.5", seq=2))
        await bus.publish(_limit_signal("40000", quantity="0.1", seq=3))

    asyncio.run(first_life())
    _assert_denied_by_max_position(store, order_events, derive_cloid("trivial:BTC:3"))

    # Second life: only the store survives. The same order, under a new signal
    # so it is judged again rather than dropped as a re-seen one, is denied too.
    second = _engine(store=store, limits=limits, start_ns=2_000)
    bus2, events2 = second.bus, second.events

    async def second_life() -> None:
        await bus2.publish(_tick("42000"))
        await bus2.publish(_limit_signal("40000", quantity="0.1", seq=4))

    asyncio.run(second_life())
    _assert_denied_by_max_position(store, events2, derive_cloid("trivial:BTC:4"))


def _a_short_of_20_under_a_lowered_cap_of_15() -> tuple[InMemoryBus, SQLiteStore, list[OrderEvent]]:
    """The engine one restart after it sold 20 with no cap, now with a cap of 15.

    A lowered cap is one way a position ends up past its cap (ADR-0051). The
    short comes from a real fill, and the second life reads it back at boot."""
    first = _engine()
    bus, store = first.bus, first.store

    async def first_life() -> None:
        await bus.publish(_tick("42000"))
        # Below the market, so the sell fills on arrival.
        await bus.publish(_limit_signal("41000", quantity="20", seq=1, side=Side.SELL))

    asyncio.run(first_life())
    assert _state(store, derive_cloid("trivial:BTC:1")) is OrderState.FILLED

    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_position=Decimal("15"))})
    second = _engine(store=store, limits=limits, start_ns=2_000)
    return second.bus, store, second.events


def test_the_oversized_short_that_shrinks_passes_the_max_position_on_paper() -> None:
    # ADR-0051's first example. Net -20 and no open buys, so a buy of 3 moves
    # the worst case from -20 to -17. That is still past 15, but it passes.
    bus, store, _ = _a_short_of_20_under_a_lowered_cap_of_15()

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("40000", quantity="3", seq=2))  # rests

    asyncio.run(scenario())
    assert _state(store, derive_cloid("trivial:BTC:2")) is OrderState.LIVE


def test_the_buy_that_crosses_zero_is_denied_on_the_new_side_on_paper() -> None:
    # ADR-0051's third example. A buy of 36 moves the worst case from -20 to
    # +16. It crosses zero, so it is judged as a +16 long, past 15.
    bus, store, order_events = _a_short_of_20_under_a_lowered_cap_of_15()

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("40000", quantity="36", seq=2))

    asyncio.run(scenario())
    _assert_denied_by_max_position(store, order_events, derive_cloid("trivial:BTC:2"))


def test_the_shrinking_sell_is_denied_by_open_sells_on_paper() -> None:
    # ADR-0051's second example, cap 15. A buy of 5 fills, then a sell of 20
    # rests. The worst case on the sell side is -15, right on the cap. A sell
    # of 3 shrinks the +5 long, but it moves the worst case to -18.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_position=Decimal("15"))})
    engine = _engine(limits=limits)
    bus, store, order_events = engine.bus, engine.store, engine.events

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        # Above the market, so the buy fills on arrival.
        await bus.publish(_limit_signal("43000", quantity="5", seq=1))
        # Above the market, so both sells would rest.
        await bus.publish(_limit_signal("44000", quantity="20", seq=2, side=Side.SELL))
        await bus.publish(_limit_signal("44000", quantity="3", seq=3, side=Side.SELL))

    asyncio.run(scenario())
    assert _state(store, derive_cloid("trivial:BTC:1")) is OrderState.FILLED
    assert _state(store, derive_cloid("trivial:BTC:2")) is OrderState.LIVE
    _assert_denied_by_max_position(store, order_events, derive_cloid("trivial:BTC:3"))


def _holding(quantity: str, *, side: Side, limits: PreTradeLimits) -> _Engine:
    """The engine one restart after it filled ``quantity`` on ``side`` with no caps.

    The caps arrive with the second life, so the position can be bigger than
    any single order they allow."""
    first = _engine()

    async def first_life() -> None:
        await first.bus.publish(_tick("42000"))
        # Priced through the market, so the order fills on arrival.
        price = "43000" if side is Side.BUY else "41000"
        await first.bus.publish(_limit_signal(price, quantity=quantity, seq=1, side=side))

    asyncio.run(first_life())
    assert _state(first.store, derive_cloid("trivial:BTC:1")) is OrderState.FILLED
    return _engine(store=first.store, limits=limits, start_ns=2_000)


def test_a_sell_that_reduces_a_long_skips_the_max_order_size() -> None:
    # Cap 0.01 and a long of 0.03. A sell of 0.04 crosses zero, so the cap
    # applies. A sell of 0.03 closes the long in one order.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_size=Decimal("0.01"))})
    engine = _holding("0.03", side=Side.BUY, limits=limits)

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        # Above the market, so a sell that passes rests LIVE.
        await engine.bus.publish(_limit_signal("44000", quantity="0.04", seq=2, side=Side.SELL))
        await engine.bus.publish(_limit_signal("44000", quantity="0.03", seq=3, side=Side.SELL))

    asyncio.run(scenario())

    _assert_denied_by(engine, derive_cloid("trivial:BTC:2"), "above max order size 0.01")
    assert _state(engine.store, derive_cloid("trivial:BTC:3")) is OrderState.LIVE


def test_an_order_that_closes_a_position_skips_the_max_order_value() -> None:
    # Cap 1000 and a mark of 40000. Closing 0.03 is worth 1320 as a sell at
    # 44000 and 1200 as a buy at 40000. Both close the position in one order.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_value=Decimal("1000"))})
    long = _holding("0.03", side=Side.BUY, limits=limits)
    short = _holding("0.03", side=Side.SELL, limits=limits)

    async def scenario(engine: _Engine, close: PlaceSignal) -> None:
        await engine.bus.publish(_tick("42000"))
        await engine.bus.publish(_mark("40000"))
        await engine.bus.publish(close)

    # Both rest on their own side of the market, so a close that passes is LIVE.
    asyncio.run(scenario(long, _limit_signal("44000", quantity="0.03", seq=2, side=Side.SELL)))
    asyncio.run(scenario(short, _limit_signal("40000", quantity="0.03", seq=2, side=Side.BUY)))

    assert _state(long.store, derive_cloid("trivial:BTC:2")) is OrderState.LIVE
    assert _state(short.store, derive_cloid("trivial:BTC:2")) is OrderState.LIVE


def test_a_sell_limit_that_reduces_a_long_needs_no_mark_under_a_max_order_value() -> None:
    # A long of 0.03 and no mark. A sell of 0.04 crosses zero, so it still needs
    # the mark. A sell of 0.03 closes the long, so it does not.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_value=Decimal("1000"))})
    engine = _holding("0.03", side=Side.BUY, limits=limits)

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))  # a trade, but no mark
        # Above the market, so a sell that passes rests LIVE.
        await engine.bus.publish(_limit_signal("44000", quantity="0.04", seq=2, side=Side.SELL))
        await engine.bus.publish(_limit_signal("44000", quantity="0.03", seq=3, side=Side.SELL))

    asyncio.run(scenario())

    _assert_denied_by(engine, derive_cloid("trivial:BTC:2"), "no mark for max order value 1000")
    assert _state(engine.store, derive_cloid("trivial:BTC:3")) is OrderState.LIVE


def test_a_sell_limit_that_reduces_a_long_passes_with_a_stale_mark() -> None:
    # The same two sells, with a mark one nanosecond past the default max age.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_value=Decimal("1000"))})
    engine = _holding("0.03", side=Side.BUY, limits=limits)
    ten_seconds = 10_000_000_000

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        await engine.bus.publish(_mark("40000", ts_ns=2_000))
        engine.clock.advance_to(2_000 + ten_seconds + 1)
        await engine.bus.publish(_limit_signal("44000", quantity="0.04", seq=2, side=Side.SELL))
        await engine.bus.publish(_limit_signal("44000", quantity="0.03", seq=3, side=Side.SELL))

    asyncio.run(scenario())

    _assert_denied_by(engine, derive_cloid("trivial:BTC:2"), "stale mark for max order value 1000")
    assert _state(engine.store, derive_cloid("trivial:BTC:3")) is OrderState.LIVE


def test_a_market_order_that_closes_a_long_needs_no_mark_under_a_max_order_value() -> None:
    # A long of 0.03 and no mark. A market sell of 0.03 closes it.
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_value=Decimal("1000"))})
    engine = _holding("0.03", side=Side.BUY, limits=limits)

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))  # a trade, but no mark
        await engine.bus.publish(_market_signal(quantity="0.03", seq=2, side=Side.SELL))

    asyncio.run(scenario())

    assert _state(engine.store, derive_cloid("trivial:BTC:2")) is OrderState.FILLED


def test_a_tripped_kill_switch_still_denies_an_order_that_closes_a_long() -> None:
    # The exemption skips only the size and value caps. A halt stops every new
    # order, closes included (ADR-0026).
    limits = PreTradeLimits(symbols={"BTC": SymbolLimits(max_order_size=Decimal("0.01"))})
    engine = _holding("0.03", side=Side.BUY, limits=limits)
    engine.guard.trip_kill_switch("operator halt")

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        await engine.bus.publish(_limit_signal("44000", quantity="0.03", seq=2, side=Side.SELL))

    asyncio.run(scenario())

    _assert_denied_by(engine, derive_cloid("trivial:BTC:2"), "kill switch tripped")


def test_kill_switch_denies_new_orders_while_resting_orders_keep_filling() -> None:
    engine = _engine()
    bus, store, guard, order_events = engine.bus, engine.store, engine.guard, engine.events
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
    assert _state(store, halted) is OrderState.DENIED
    # The resting order rode through the halt: it went LIVE and then FILLED.
    assert any(isinstance(ev, OrderLive) and ev.cloid == resting for ev in order_events)
    assert _state(store, resting) is OrderState.FILLED


_THREE_PER_SECOND = PreTradeLimits(max_orders_per_window=3, window_seconds=1.0)
_RATE_CAP_REASON = "above max orders per window 3 in 1.0s"


def test_the_fourth_placement_inside_one_second_is_denied_by_the_rate_cap() -> None:
    engine = _engine(limits=_THREE_PER_SECOND)

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        # All four rest below the market, so the first three end LIVE.
        for seq in range(1, 5):
            await engine.bus.publish(_limit_signal("100", seq=seq))

    asyncio.run(scenario())

    for seq in range(1, 4):
        assert _state(engine.store, derive_cloid(f"trivial:BTC:{seq}")) is OrderState.LIVE
    _assert_denied_by(engine, derive_cloid("trivial:BTC:4"), _RATE_CAP_REASON)


def test_a_placement_passes_again_once_the_oldest_slot_leaves_the_window() -> None:
    engine = _engine(limits=_THREE_PER_SECOND)
    one_second = 1_000_000_000

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        await engine.bus.publish(_limit_signal("100", seq=1))
        engine.clock.advance_to(1_000 + 500_000_000)
        await engine.bus.publish(_limit_signal("100", seq=2))
        await engine.bus.publish(_limit_signal("100", seq=3))
        engine.clock.advance_to(1_000 + one_second - 1)  # the first slot is still inside
        await engine.bus.publish(_limit_signal("100", seq=4))
        engine.clock.advance_to(1_000 + one_second)  # the first slot has just left
        await engine.bus.publish(_limit_signal("100", seq=5))
        # Only the first slot left. The other two still fill the window with seq 5.
        await engine.bus.publish(_limit_signal("100", seq=6))

    asyncio.run(scenario())

    _assert_denied_by(engine, derive_cloid("trivial:BTC:4"), _RATE_CAP_REASON)
    assert _state(engine.store, derive_cloid("trivial:BTC:5")) is OrderState.LIVE
    _assert_denied_by(engine, derive_cloid("trivial:BTC:6"), _RATE_CAP_REASON)


def test_orders_on_two_symbols_share_one_rate_cap_window() -> None:
    # A venue rate-limits the account, not a symbol, so the window is engine-wide.
    engine = _engine(limits=_THREE_PER_SECOND)

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        await engine.bus.publish(_tick("2500", symbol="ETH"))
        await engine.bus.publish(_limit_signal("100", seq=1))
        await engine.bus.publish(_limit_signal("100", seq=2))
        await engine.bus.publish(_limit_signal("100", seq=1, symbol="ETH"))
        await engine.bus.publish(_limit_signal("100", seq=2, symbol="ETH"))

    asyncio.run(scenario())

    for cloid in ("trivial:BTC:1", "trivial:BTC:2", "trivial:ETH:1"):
        assert _state(engine.store, derive_cloid(cloid)) is OrderState.LIVE
    _assert_denied_by(engine, derive_cloid("trivial:ETH:2"), _RATE_CAP_REASON)


def test_an_order_denied_by_another_cap_takes_no_rate_cap_slot() -> None:
    limits = PreTradeLimits(
        symbols={"BTC": SymbolLimits(max_order_size=Decimal("0.5"))},
        max_orders_per_window=3,
        window_seconds=1.0,
    )
    engine = _engine(limits=limits)

    async def scenario() -> None:
        await engine.bus.publish(_tick("42000"))
        await engine.bus.publish(_limit_signal("100", seq=1))
        await engine.bus.publish(_limit_signal("100", quantity="0.6", seq=2))  # too big
        await engine.bus.publish(_limit_signal("100", seq=3))
        await engine.bus.publish(_limit_signal("100", seq=4))
        await engine.bus.publish(_limit_signal("100", seq=5))

    asyncio.run(scenario())

    _assert_denied_by(engine, derive_cloid("trivial:BTC:2"), "above max order size 0.5")
    # The size denial left its slot free, so the fourth order still fits.
    for seq in (1, 3, 4):
        assert _state(engine.store, derive_cloid(f"trivial:BTC:{seq}")) is OrderState.LIVE
    _assert_denied_by(engine, derive_cloid("trivial:BTC:5"), _RATE_CAP_REASON)
