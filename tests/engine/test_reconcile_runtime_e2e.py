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
from decimal import Decimal
from pathlib import Path

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    ComponentState,
    MarketTick,
    OrderEvent,
    OrderFilled,
    OrderState,
    Side,
    derive_cloid,
)
from tickwright.engine.reconcile import ReconcileConfig
from tickwright.engine.runner import Engine, EngineConfig
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
