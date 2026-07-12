"""Continuous reconciliation runtime E2E (issue #49): the in-flight/open-order
cadences scheduled by the runner, paced by feed-driven virtual time (ADR-0033).

The venue is a real ``PaperExchange`` constructed on a *private* bus, so every
ack and fill it reports is lost to the engine — the severed report link from
the crash-recovery suite, held open for a whole run. The engine can then only
converge through the continuous reconciliation cadences: the replayed ticks
advance ``ManualClock`` past the cadence deadlines, the cycles fetch venue
truth, and the heals ride the engine bus as ``reconciliation``-flagged
replicas. Zero external services; the whole run is deterministic.
"""

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    ComponentState,
    InstrumentSpec,
    MarketTick,
    OrderEvent,
    OrderFilled,
    OrderRejected,
    OrderState,
    PlaceOrder,
    Side,
    VenueOrderView,
    derive_cloid,
)
from tickwright.engine.reconcile import ReconcileConfig
from tickwright.engine.runner import Engine, EngineConfig
from tickwright.observability.testing import capture_events
from tickwright.strategies import SingleShotLimitStrategy

_NS = 1_000_000_000
_CLOID = derive_cloid("trivial:BTC:1")


def _ticks_file(path: Path, rows: list[tuple[str, int]]) -> Path:
    lines = [
        json.dumps(
            {
                "symbol": "BTC",
                "price": price,
                "size": "3",
                "aggressor_side": "sell",
                "trade_id": chr(ord("a") + i),
                "ts_event": ts_event,
            }
        )
        for i, (price, ts_event) in enumerate(rows)
    ]
    path.write_text("\n".join(lines) + "\n")
    return path


def test_a_missed_fill_landing_mid_run_is_healed_by_the_inflight_cadence(
    tmp_path: Path,
) -> None:
    """ADR-0011's continuous net, live in the composed engine: an order whose
    ack and fill both vanished (venue on a dead report link) is healed to
    ``FILLED`` while the engine runs — no restart, no manual reconcile call —
    because the runner schedules ``reconcile_inflight`` and the replayed ticks
    drive virtual time across its deadline."""
    ticks = _ticks_file(
        tmp_path / "ticks.jsonl",
        [
            ("42000", 1 * _NS),  # strategy places its resting limit BUY @41000
            ("40000", 2 * _NS),  # the market crosses at the venue: fill, ack lost
            ("45000", 8 * _NS),  # drives time past the 5s in-flight deadline
        ],
    )
    db = tmp_path / "saga.db"

    async def main() -> tuple[int, Engine, list[OrderEvent]]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(db)
        # The severed report link: the venue acks and fills into its own
        # private bus, so the engine sees nothing it doesn't reconcile for.
        venue_bus = InMemoryBus()
        venue = PaperExchange(bus=venue_bus, clock=clock, fill_model=ImmediateFillModel())
        feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=venue,
            feed=feed,
            config=EngineConfig(reconcile=ReconcileConfig()),
        )
        strategy = SingleShotLimitStrategy(
            strategy_id="trivial",
            bus=bus,
            clock=clock,
            side=Side.BUY,
            quantity=Decimal("0.5"),
            price=Decimal("41000"),
        )
        engine.register(strategy, symbols={"BTC"})
        # The venue self-subscribes to *its* bus at construction; the severed
        # link splits the buses, so the tick wire stays explicit here (the
        # same split as the crash-recovery suite).
        bus.subscribe(MarketTick, venue.on_tick)

        events: list[OrderEvent] = []
        healed = asyncio.Event()

        async def record(event: OrderEvent) -> None:
            events.append(event)
            if isinstance(event, OrderFilled):
                healed.set()

        bus.subscribe(OrderEvent, record)

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(healed.wait(), timeout=5)
        assert engine.state is ComponentState.RUNNING
        await engine.stop()
        return await run, engine, events

    exit_code, engine, events = asyncio.run(main())

    assert exit_code == 0
    assert engine.state is ComponentState.STOPPED

    # The durable saga converged on the venue's executed truth, mid-run.
    reopened = SQLiteStore(db)
    try:
        order = reopened.get_order(_CLOID)
        assert order is not None
        assert order.state is OrderState.FILLED
        assert order.cum_qty == Decimal("0.5")
    finally:
        reopened.close()

    # Every heal is a provenance-flagged replica routed through the saga.
    healing = [ev for ev in events if ev.reconciliation]
    assert any(isinstance(ev, OrderFilled) for ev in healing)


class _VanishingLinkExchange:
    """The venue link for the ghost scenario, its behavior keyed off *virtual*
    time so the whole script stays deterministic: delegate normally while the
    order settles, then read as vanished (a positive no-record view), with one
    outage window in the middle where the read itself fails (``None``). A
    network boundary is the one place a test double is allowed."""

    def __init__(self, venue: PaperExchange, clock: ManualClock) -> None:
        self._venue = venue
        self._clock = clock

    async def place(self, order: PlaceOrder) -> None:
        await self._venue.place(order)

    async def cancel(self, cloid: str) -> None:
        await self._venue.cancel(cloid)

    def instrument_specs(self) -> Mapping[str, InstrumentSpec]:
        return self._venue.instrument_specs()

    async def fetch_order(self, cloid: str) -> VenueOrderView | None:
        now_s = self._clock.timestamp_ns() / _NS
        if now_s < 3:
            return await self._venue.fetch_order(cloid)  # settled and acked
        if 10 <= now_s < 12:
            return None  # the outage: a failed read, never "no record"
        return VenueOrderView(status=None)  # vanished from the venue


def test_a_vanished_order_is_ghosted_only_after_grace_and_a_none_read_freezes(
    tmp_path: Path,
) -> None:
    """ADR-0011's ghost discipline, live in the composed engine: a resting
    order that vanishes mid-run is resolved terminally only after continuous
    absence across the grace window (every read doubling as the fill-history
    cross-check), and the outage read in the middle freezes that cycle and
    removes nothing — the frozen pass provably precedes the ghost verdict."""
    config = ReconcileConfig(
        inflight_interval_seconds=2.0,
        inflight_max_attempts=3,
        open_order_interval_seconds=5.0,
        ghost_grace_seconds=20.0,
        recent_order_protection_seconds=4.0,
    )
    # The open-order cadence fires on feed-driven deadlines at t=6, 11 (the
    # outage → frozen), 16, 21 (absent but inside grace: WAITING), and t=27 —
    # where absence since t=6 finally crosses the 20s grace window.
    ticks = _ticks_file(
        tmp_path / "ticks.jsonl",
        [("42000", t * _NS) for t in (1, 6, 11, 16, 21, 27)],
    )
    db = tmp_path / "saga.db"

    async def main() -> tuple[int, Engine, list[OrderEvent]]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(db)
        venue = PaperExchange(bus=bus, clock=clock, fill_model=ImmediateFillModel())
        feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=_VanishingLinkExchange(venue, clock),
            feed=feed,
            config=EngineConfig(reconcile=config),
        )
        strategy = SingleShotLimitStrategy(
            strategy_id="trivial",
            bus=bus,
            clock=clock,
            side=Side.BUY,
            quantity=Decimal("0.5"),
            price=Decimal("41000"),  # rests below the 42000 prints: never fills
        )
        engine.register(strategy, symbols={"BTC"})

        events: list[OrderEvent] = []
        ghosted = asyncio.Event()

        async def record(event: OrderEvent) -> None:
            events.append(event)
            if isinstance(event, OrderRejected):
                ghosted.set()

        bus.subscribe(OrderEvent, record)

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(ghosted.wait(), timeout=5)
        assert engine.state is ComponentState.RUNNING
        await engine.stop()
        return await run, engine, events

    with capture_events() as logs:
        exit_code, engine, events = asyncio.run(main())

    assert exit_code == 0
    assert engine.state is ComponentState.STOPPED

    # Ghosted exactly once, to REJECTED (LIVE, no fills, no requested cancel),
    # and only at t=27 — the first cycle past the grace window. An earlier
    # resolution (t=16/21, or during the frozen pass) would land an earlier ts.
    verdicts = [ev for ev in events if isinstance(ev, OrderRejected)]
    assert len(verdicts) == 1
    assert verdicts[0].reconciliation
    assert verdicts[0].ts_event == 27 * _NS

    # The outage froze a cycle — and removed nothing: the freeze precedes the
    # ghost verdict in the observable trail.
    names = [str(log["event"]) for log in logs]
    assert "reconcile.frozen" in names
    assert names.index("reconcile.frozen") < names.index("ghost.reconciled")

    # The durable saga carries the terminal resolution across the stop.
    reopened = SQLiteStore(db)
    try:
        order = reopened.get_order(_CLOID)
        assert order is not None
        assert order.state is OrderState.REJECTED
    finally:
        reopened.close()
