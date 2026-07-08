"""Property suites: the saga converges under at-least-once delivery (ADR-0002).

Everything here runs through real components — ``InMemoryBus``,
``ExecutionManager``, ``PaperExchange``, ``SQLiteStore(":memory:")`` — with
Hypothesis actively injecting the duplicates a redelivering bus would produce:
resent ``Signal``s, redelivered ``FillReport``s, and multi-part fills with
copies interleaved. Convergence means the duplicated run ends in exactly the
baseline's saga state, with no extra order placement and no double-counted
``cum_qty``.
"""

import asyncio
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AggressorSide,
    ExecutionReport,
    FillReport,
    MarketTick,
    OrderEvent,
    OrderFillEvent,
    OrderState,
    OrderType,
    PlaceSignal,
    Side,
    Signal,
    TimeInForce,
    derive_cloid,
)
from tickwright.engine.cache import Cache
from tickwright.engine.execution import ExecutionManager

_CLOID = derive_cloid("trivial:BTC:1")


def _wire() -> tuple[InMemoryBus, SQLiteStore, list[OrderEvent]]:
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    store = SQLiteStore(":memory:")
    exchange = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
    cache = Cache(store=store)
    manager = ExecutionManager(bus=bus, clock=clock, exchange=exchange, cache=cache)

    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)

    order_events: list[OrderEvent] = []

    async def record(event: OrderEvent) -> None:
        order_events.append(event)

    bus.subscribe(OrderEvent, record)
    return bus, store, order_events


def _signal(quantity: Decimal = Decimal("0.5")) -> PlaceSignal:
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=1,
        side=Side.BUY,
        quantity=quantity,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def _tick() -> MarketTick:
    return MarketTick(
        ts_event=1_000,
        ts_init=1_000,
        symbol="BTC",
        price=Decimal("42000"),
        size=Decimal("10"),
        aggressor_side=AggressorSide.BUY,
        trade_id="t1",
        seq=0,
    )


def _fill_report(trade_id: str, quantity: Decimal) -> FillReport:
    return FillReport(
        ts_event=1_000,
        ts_init=1_000,
        cloid=_CLOID,
        symbol="BTC",
        trade_id=trade_id,
        quantity=quantity,
        price=Decimal("42000"),
    )


def _fingerprint(events: list[OrderEvent]) -> list[tuple[str, str]]:
    return [(type(ev).__name__, ev.event_id) for ev in events]


def _run_with_redeliveries(plan: list[str]) -> tuple[SQLiteStore, list[OrderEvent]]:
    """One happy-path placement, then redeliver duplicates per ``plan``."""
    bus, store, order_events = _wire()

    async def scenario() -> None:
        await bus.publish(_tick())
        await bus.publish(_signal())
        for kind in plan:
            if kind == "signal":
                await bus.publish(_signal())
            else:
                # The exact fill the paper exchange emitted, redelivered.
                await bus.publish(_fill_report(f"{_CLOID}-1", Decimal("0.5")))

    asyncio.run(scenario())
    return store, order_events


@given(plan=st.lists(st.sampled_from(["signal", "fill"]), max_size=6))
def test_duplicate_signals_and_reports_converge_to_the_baseline_saga(plan: list[str]) -> None:
    baseline_store, baseline_events = _run_with_redeliveries([])
    store, order_events = _run_with_redeliveries(plan)

    record = store.get_order(_CLOID)
    baseline = baseline_store.get_order(_CLOID)
    assert record is not None and baseline is not None
    assert record.state is baseline.state is OrderState.FILLED
    assert record.cum_qty == baseline.cum_qty == Decimal("0.5")

    # The canonical OrderEvent stream is identical — duplicates left no trace.
    assert _fingerprint(order_events) == _fingerprint(baseline_events)

    # No extra placement: the paper exchange mints {cloid}-N per place() call,
    # so a second send would surface as a second distinct trade id.
    trades = {ev.trade_id for ev in order_events if isinstance(ev, OrderFillEvent)}
    assert trades == {f"{_CLOID}-1"}


@given(
    parts=st.lists(
        st.tuples(st.integers(min_value=1, max_value=5), st.integers(min_value=1, max_value=3)),
        min_size=1,
        max_size=4,
    ),
    data=st.data(),
)
def test_duplicated_fill_reports_never_double_count_cum_qty(
    parts: list[tuple[int, int]], data: st.DataObject
) -> None:
    quantities = [Decimal(tenths) / 10 for tenths, _ in parts]
    total = sum(quantities, Decimal("0"))

    # Each part is delivered `copies` times; Hypothesis interleaves the multiset.
    deliveries = [
        _fill_report(f"f{index}", quantity)
        for index, ((_, copies), quantity) in enumerate(zip(parts, quantities, strict=True))
        for _ in range(copies)
    ]
    deliveries = data.draw(st.permutations(deliveries))

    bus, store, order_events = _wire()

    async def scenario() -> None:
        # No tick cached: the send crashes, leaving an in-flight saga the
        # venue's (duplicated) fill stream then resolves.
        with pytest.raises(ValueError):
            await bus.publish(_signal(quantity=total))
        for report in deliveries:
            await bus.publish(report)

    asyncio.run(scenario())

    record = store.get_order(_CLOID)
    assert record is not None
    assert record.state is OrderState.FILLED
    assert record.cum_qty == total

    # Each unique trade_id counted exactly once, however many copies arrived.
    fills = [ev for ev in order_events if isinstance(ev, OrderFillEvent)]
    assert sum(ev.quantity for ev in fills) == total
    assert len(fills) == len(parts)
