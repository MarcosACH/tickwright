"""``Reconciler`` continuous loops — the running correctness net (ADR-0011).

After the startup barrier clears, two periodic cycles keep local saga state
converged on venue truth: a fast in-flight check resolving ``SUBMITTED`` orders
that never acked, and a slower open-order/ghost reconcile for resting orders.
Heals ride the same reconciliation-flagged synthetic events as startup, routed
through the ``ExecutionManager`` so dedup makes every cycle idempotent.
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
    MarketTick,
    Order,
    OrderEvent,
    OrderLive,
    OrderState,
    OrderType,
    PlaceOrder,
    Side,
    Signal,
    TimeInForce,
)
from tickwright.engine.cache import Cache
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.reconcile import Reconciler


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
    store: SQLiteStore, exchange: PaperExchange, clock: ManualClock
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
    reconciler = Reconciler(bus=bus, clock=clock, exchange=exchange, cache=cache)
    return cache, reconciler, events


async def _record(sink: list[OrderEvent], event: OrderEvent) -> None:
    sink.append(event)


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
