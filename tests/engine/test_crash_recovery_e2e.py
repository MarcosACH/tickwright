"""Crash-recovery E2E (issue #14): kill after the ``PENDING`` checkpoint, restart,
and converge — through the whole real pipeline.

First life is the real wiring — ``ReplayFeed`` tick → ``SingleShotLimitStrategy``
→ ``ExecutionManager`` → venue — killed inside the send window by a transport
that dies mid-``place`` (the one crash ADR-0008 calls irreducible). The venue is
a real ``PaperExchange`` playing the remote venue: its report link (its bus) and
any acks on it die with the process; its own state survives, exactly like a real
venue outliving our crash. The second life rebuilds the ``Cache`` from the
surviving ``Store``, runs the startup barrier, and must end with **no duplicate
placement for the cloid and no lost fill** — with an at-least-once redelivery of
the original signal thrown in.
"""

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AggressorSide,
    ExecutionReport,
    InstrumentSpec,
    MarketTick,
    OrderEvent,
    OrderFailed,
    OrderFilled,
    OrderPlaced,
    OrderState,
    OrderSubmitted,
    OrderType,
    PlaceOrder,
    PlaceSignal,
    Side,
    Signal,
    TimeInForce,
    VenueOrderView,
    derive_cloid,
)
from tickwright.engine.cache import Cache
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.reconcile import Reconciler
from tickwright.strategies import SingleShotLimitStrategy

_CLOID = derive_cloid("trivial:BTC:1")


class _CrashingTransport:
    """The venue link for the doomed first life: the process dies in the send
    window. ``pre_send=True`` crashes before the request reaches the venue;
    otherwise the venue receives it and only the ack is lost (ADR-0008's
    irreducible window). A real network boundary is the one place a test double
    is allowed."""

    def __init__(self, venue: PaperExchange, *, pre_send: bool) -> None:
        self._venue = venue
        self._pre_send = pre_send

    async def place(self, order: PlaceOrder) -> None:
        if self._pre_send:
            raise ConnectionError("process died before the send left the box")
        await self._venue.place(order)
        raise ConnectionError("process died with the send in flight; ack lost")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("first life never cancels")

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        raise AssertionError("first life never fetches")

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        return self._venue.instrument_specs()


def _ticks_file(path: Path, prices: list[str]) -> Path:
    rows = [
        {
            "symbol": "BTC",
            "price": price,
            "size": "3",
            "aggressor_side": "sell",
            "trade_id": chr(ord("a") + i),
            "ts_event": 1_000 * (i + 1),
        }
        for i, price in enumerate(prices)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def _crossing_tick(ts: int = 5_000) -> MarketTick:
    return MarketTick(
        ts_event=ts,
        ts_init=ts,
        symbol="BTC",
        price=Decimal("40000"),
        size=Decimal("3"),
        aggressor_side=AggressorSide.SELL,
        trade_id="dead-window",
        seq=9,
    )


def _redelivered_signal() -> PlaceSignal:
    """The strategy's original signal, redelivered across the restart."""
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=1,
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal("41000"),
    )


def _first_life(
    tmp_path: Path, *, pre_send: bool
) -> tuple[SQLiteStore, PaperExchange, ManualClock]:
    """Run the real pipeline into the crash; return what survives it."""
    bus = InMemoryBus()
    clock = ManualClock()
    store = SQLiteStore(":memory:")
    venue_bus = InMemoryBus()  # the venue's report link — dies with the process
    venue = PaperExchange(bus=venue_bus, clock=clock, fill_model=ImmediateFillModel())
    cache = Cache(store=store)
    manager = ExecutionManager(
        bus=bus, clock=clock, exchange=_CrashingTransport(venue, pre_send=pre_send), cache=cache
    )
    strategy = SingleShotLimitStrategy(
        strategy_id="trivial",
        bus=bus,
        clock=clock,
        side=Side.BUY,
        quantity=Decimal("0.5"),
        price=Decimal("41000"),
    )
    feed = ReplayFeed(path=_ticks_file(tmp_path / "ticks.jsonl", ["42000"]), bus=bus, clock=clock)

    bus.subscribe(MarketTick, venue.on_tick)
    bus.subscribe(MarketTick, strategy.on_tick)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)

    with pytest.raises(ConnectionError):
        asyncio.run(feed.start())

    # The write-ahead intent is the durable truth the crash left behind.
    record = store.get_order(_CLOID)
    assert record is not None and record.state is OrderState.PENDING
    return store, venue, clock


def _second_life(
    store: SQLiteStore, venue: PaperExchange, clock: ManualClock
) -> tuple[InMemoryBus, Reconciler, list[OrderEvent]]:
    """Recovery wiring over the survivors: rebuilt Cache, fresh bus and manager."""
    bus = InMemoryBus()
    cache = Cache(store=store)
    cache.rebuild()
    manager = ExecutionManager(bus=bus, clock=clock, exchange=venue, cache=cache)
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)
    events: list[OrderEvent] = []

    async def record(event: OrderEvent) -> None:
        events.append(event)

    bus.subscribe(OrderEvent, record)
    reconciler = Reconciler(bus=bus, clock=clock, exchange=venue, cache=cache)
    return bus, reconciler, events


def test_post_send_kill_recovers_the_landed_order_and_its_fill(tmp_path: Path) -> None:
    store, venue, clock = _first_life(tmp_path, pre_send=False)

    # While we are dead the market crosses the resting BUY at the venue: it
    # fills, reporting into the dead link.
    asyncio.run(venue.on_tick(_crossing_tick()))

    bus, reconciler, events = _second_life(store, venue, clock)
    asyncio.run(reconciler.run_startup_barrier(timeout_seconds=30.0))

    # No lost fill: the saga converged on the venue's executed truth.
    recovered = store.get_order(_CLOID)
    assert recovered is not None
    assert recovered.state is OrderState.FILLED
    assert recovered.cum_qty == Decimal("0.5")
    assert [type(ev) for ev in events] == [OrderSubmitted, OrderFilled]
    assert all(ev.reconciliation for ev in events)

    # At-least-once redelivery of the original signal: no duplicate placement.
    asyncio.run(bus.publish(_redelivered_signal()))
    view = asyncio.run(venue.fetch_order(_CLOID))
    assert view is not None
    assert [fill.trade_id for fill in view.fills] == [f"{_CLOID}-1"]
    assert [type(ev) for ev in events] == [OrderSubmitted, OrderFilled]


def test_pre_send_kill_resolves_the_unlanded_intent_failed_never_resent(
    tmp_path: Path,
) -> None:
    store, venue, clock = _first_life(tmp_path, pre_send=True)

    bus, reconciler, events = _second_life(store, venue, clock)
    asyncio.run(reconciler.run_startup_barrier(timeout_seconds=30.0))

    # The venue never saw the cloid: proven never-landed → FAILED (ADR-0010).
    recovered = store.get_order(_CLOID)
    assert recovered is not None
    assert recovered.state is OrderState.FAILED
    assert [type(ev) for ev in events] == [OrderFailed]

    # Redelivery converges on the terminal saga — nothing placed, ever.
    asyncio.run(bus.publish(_redelivered_signal()))
    assert [type(ev) for ev in events] == [OrderFailed]
    assert not [ev for ev in events if isinstance(ev, OrderPlaced)]
    view = asyncio.run(venue.fetch_order(_CLOID))
    assert view is not None and not view.has_record
