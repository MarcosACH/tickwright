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

from ledgers import GENESIS
from venue_doubles import VenueLink

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AggressorSide,
    ComponentState,
    ExternalOrderRef,
    MarketTick,
    OrderEvent,
    OrderFailed,
    OrderFilled,
    OrderLive,
    OrderRef,
    OrderRejected,
    OrderState,
    PlaceOrder,
    Side,
    VenueOpenOrder,
    VenueOrderView,
    VenueReadFailure,
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
        venue = PaperExchange(
            bus=venue_bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
            applied_fills=lambda cloid: (),
            resting_orders=list,
        )
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
            emitter=engine.emitter_for("trivial"),
            portfolio=engine.portfolio_for("trivial"),
            settings=SingleShotLimitStrategy.Settings(
                side=Side.BUY,
                quantity=Decimal("0.5"),
                price=Decimal("41000"),
            ),
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


class _VanishingLinkExchange(VenueLink):
    """The venue link for the ghost scenario, its behavior keyed off *virtual*
    time so the whole script stays deterministic: delegate normally while the
    order settles, then read as vanished (a positive no-record view), with one
    outage window in the middle where the send itself dies. A
    network boundary is the one place a test double is allowed."""

    def __init__(self, venue: PaperExchange, clock: ManualClock) -> None:
        super().__init__(venue)
        self._clock = clock

    async def fetch_order(self, ref: OrderRef) -> VenueOrderView | VenueReadFailure:
        now_s = self._clock.timestamp_ns() / _NS
        if now_s < 3:
            return await self._venue.fetch_order(ref)  # settled and acked
        if 10 <= now_s < 12:
            # The outage: a failed *send*, never "no record" — and never the
            # unreadable body beside it, which the venue is up to answer.
            return VenueReadFailure.SEND_FAILED
        return VenueOrderView(status=None)  # vanished from the venue


def test_a_vanished_order_is_ghosted_only_after_grace_and_a_failed_read_freezes(
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
        venue = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
            applied_fills=lambda cloid: (),
            resting_orders=list,
        )
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
            emitter=engine.emitter_for("trivial"),
            portfolio=engine.portfolio_for("trivial"),
            settings=SingleShotLimitStrategy.Settings(
                side=Side.BUY,
                quantity=Decimal("0.5"),
                price=Decimal("41000"),  # rests below the 42000 prints: never fills
            ),
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


class _GarbledLinkExchange(VenueLink):
    """A venue that answers every order read with a body the adapter cannot
    read — the durable shape of #236, where the venue is up and its stored
    value never changes. A network boundary is the one place a double is
    allowed."""

    async def fetch_order(self, ref: OrderRef) -> VenueOrderView | VenueReadFailure:
        return VenueReadFailure.UNREADABLE_BODY


def test_a_durably_unreadable_body_faults_the_composed_engine(tmp_path: Path) -> None:
    """The escalation's whole claim, in the composed runtime: the fault reaches
    the ``TaskGroup`` and stops the process (ADR-0024/0049).

    Nothing between the reconciler and the runner may absorb it. Its
    predecessor is the permanent silent freeze — an engine that keeps running,
    keeps trading and stops reconciling — which is the failure an operator
    cannot see, and the one this exists to replace.
    """
    config = ReconcileConfig(
        inflight_interval_seconds=2.0,
        inflight_max_attempts=3,
        open_order_interval_seconds=5.0,
        ghost_grace_seconds=20.0,
        recent_order_protection_seconds=4.0,
        unreadable_grace_seconds=10.0,
    )
    # The open-order cadence fires at t=6, 11 and 16: the first unreadable read
    # starts the span and the last is 10s past it, so the one resting order is
    # continuously unreadable across the whole of it.
    ticks = _ticks_file(
        tmp_path / "ticks.jsonl",
        [("42000", t * _NS) for t in (1, 6, 11, 16, 21)],
    )

    async def main() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        venue = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
            applied_fills=lambda cloid: (),
            resting_orders=list,
        )
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=_GarbledLinkExchange(venue),
            feed=ReplayFeed(path=ticks, bus=bus, clock=clock),
            config=EngineConfig(reconcile=config),
        )
        engine.register(
            SingleShotLimitStrategy(
                emitter=engine.emitter_for("trivial"),
                portfolio=engine.portfolio_for("trivial"),
                settings=SingleShotLimitStrategy.Settings(
                    side=Side.BUY,
                    quantity=Decimal("0.5"),
                    price=Decimal("41000"),  # rests below the 42000 prints: never fills
                ),
            ),
            symbols={"BTC"},
        )
        return await asyncio.wait_for(engine.run(), timeout=5), engine

    with capture_events() as logs:
        exit_code, engine = asyncio.run(main())

    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED

    # No new named event (ADR-0045's catalog is closed): the ordinary
    # `engine.faulted` carries the refusal's own repr, cloid included. The
    # refusal is raised inside a supervised task, so the repr is the fault's
    # and not the TaskGroup wrapper's (#338).
    (faulted,) = [log for log in logs if log["event"] == "engine.faulted"]
    assert faulted["error"].startswith("VenueReadUnresolvable(")
    assert _CLOID in faulted["error"]

    # It escalated rather than freezing forever: the budget's freezes are there,
    # bounded by it, and never a fourth cycle's.
    assert len([log for log in logs if log["event"] == "reconcile.frozen"]) == 2


class _SlowSendExchange(VenueLink):
    """A venue whose place request stays on the wire until the test lets it
    land, like the slow testnet POST of #640. A network boundary is the one
    place a double is allowed."""

    def __init__(self, venue: PaperExchange, *, delivers: bool = True) -> None:
        super().__init__(venue)
        self._delivers = delivers
        self.sending = asyncio.Event()
        self.land = asyncio.Event()

    async def place(self, order: PlaceOrder) -> None:
        self.sending.set()
        await self.land.wait()
        if self._delivers:
            await self._venue.place(order)


async def _let_cadences_run() -> None:
    for _ in range(50):
        await asyncio.sleep(0)


def _slow_send_engine(
    tmp_path: Path, *, delivers: bool
) -> tuple[Engine, ManualClock, InMemoryBus, _SlowSendExchange]:
    """An engine whose strategy places one resting BUY at t=1, through a send
    the test holds open. The saga store is ``tmp_path / "saga.db"``."""
    ticks = _ticks_file(tmp_path / "ticks.jsonl", [("42000", 1 * _NS)])
    bus = InMemoryBus()
    clock = ManualClock()
    venue = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
        applied_fills=lambda cloid: (),
        resting_orders=list,
    )
    link = _SlowSendExchange(venue, delivers=delivers)
    engine = Engine(
        bus=bus,
        clock=clock,
        store=SQLiteStore(tmp_path / "saga.db"),
        exchange=link,
        feed=ReplayFeed(path=ticks, bus=bus, clock=clock),
        config=EngineConfig(reconcile=ReconcileConfig()),
    )
    strategy = SingleShotLimitStrategy(
        emitter=engine.emitter_for("trivial"),
        portfolio=engine.portfolio_for("trivial"),
        settings=SingleShotLimitStrategy.Settings(
            side=Side.BUY,
            quantity=Decimal("0.5"),
            price=Decimal("41000"),  # rests below the 42000 print
        ),
    )
    engine.register(strategy, symbols={"BTC"})
    return engine, clock, bus, link


def test_an_order_is_not_failed_while_its_send_is_still_open(tmp_path: Path) -> None:
    """#640: the venue accepted an order 15.3 s after the send began, and the
    in-flight pass had already called it ``FAILED`` after three empty reads. A
    "no record" read while the send is open is not proof the send failed."""

    async def main() -> list[OrderEvent]:
        engine, clock, bus, link = _slow_send_engine(tmp_path, delivers=True)
        events: list[OrderEvent] = []
        live = asyncio.Event()

        async def record(event: OrderEvent) -> None:
            events.append(event)
            if isinstance(event, OrderLive):
                live.set()

        bus.subscribe(OrderEvent, record)

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(link.sending.wait(), timeout=5)
        # Five in-flight deadlines pass with the send still open. The default
        # budget is three reads, so the old engine gave up at the third.
        for t in (6, 11, 16, 21, 26):
            clock.advance_to(t * _NS)
            await _let_cadences_run()
        link.land.set()
        await asyncio.wait_for(live.wait(), timeout=5)
        await engine.stop()
        await run
        return events

    events = asyncio.run(main())

    assert not any(isinstance(ev, OrderFailed) for ev in events)
    reopened = SQLiteStore(tmp_path / "saga.db")
    try:
        order = reopened.get_order(_CLOID)
        assert order is not None
        assert order.state is OrderState.LIVE
    finally:
        reopened.close()


def test_a_send_that_returned_with_no_record_still_fails_after_the_budget(
    tmp_path: Path,
) -> None:
    """The other half of #640: the budget waits for the send, but it still
    ends. Here the send returns at t=12 and the venue never got the order. The
    three misses count from the reads at t=16, 21 and 26."""

    async def main() -> list[OrderEvent]:
        engine, clock, bus, link = _slow_send_engine(tmp_path, delivers=False)
        events: list[OrderEvent] = []
        failed = asyncio.Event()

        async def record(event: OrderEvent) -> None:
            events.append(event)
            if isinstance(event, OrderFailed):
                failed.set()

        bus.subscribe(OrderEvent, record)

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(link.sending.wait(), timeout=5)
        for t in (6, 11):
            clock.advance_to(t * _NS)
            await _let_cadences_run()
        clock.advance_to(12 * _NS)
        link.land.set()
        await _let_cadences_run()
        for t in (16, 21, 26):
            clock.advance_to(t * _NS)
            await _let_cadences_run()
        await asyncio.wait_for(failed.wait(), timeout=5)
        await engine.stop()
        await run
        return events

    events = asyncio.run(main())

    (verdict,) = [ev for ev in events if isinstance(ev, OrderFailed)]
    assert verdict.reconciliation
    # The third miss after the send ended. Misses during the send would
    # have failed it at t=16.
    assert verdict.ts_event == 26 * _NS


class _ListRecordingExchange(VenueLink):
    """The real paper venue, with each open-order list it answers kept, so the
    test can tell a pass that ran clean from a pass that never ran."""

    def __init__(self, venue: PaperExchange) -> None:
        super().__init__(venue)
        self.lists: list[list[VenueOpenOrder | ExternalOrderRef] | VenueReadFailure] = []

    async def fetch_open_orders(self) -> list[VenueOpenOrder | ExternalOrderRef] | VenueReadFailure:
        listed = await self._venue.fetch_open_orders()
        self.lists.append(listed)
        return listed


def test_the_stray_check_runs_clean_on_the_paper_path(tmp_path: Path) -> None:
    """#642 on the hermetic path. Paper has no stray order to find, since a
    saga stays open while its send is (#640). So every open-order pass must
    read the list, find nothing to act on, and log nothing. A resting order
    under a working saga is not a stray, and a filled one is not listed."""
    ticks = _ticks_file(tmp_path / "ticks.jsonl", [("42000", 1 * _NS)])

    async def main() -> tuple[Engine, _ListRecordingExchange, int]:
        bus = InMemoryBus()
        clock = ManualClock()
        venue = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
            applied_fills=lambda cloid: (),
            resting_orders=list,
        )
        link = _ListRecordingExchange(venue)
        engine = Engine(
            bus=bus,
            clock=clock,
            store=SQLiteStore(tmp_path / "saga.db"),
            exchange=link,
            feed=ReplayFeed(path=ticks, bus=bus, clock=clock),
            config=EngineConfig(reconcile=ReconcileConfig()),
        )
        strategy = SingleShotLimitStrategy(
            emitter=engine.emitter_for("trivial"),
            portfolio=engine.portfolio_for("trivial"),
            settings=SingleShotLimitStrategy.Settings(
                side=Side.BUY, quantity=Decimal("0.5"), price=Decimal("41000")
            ),
        )
        engine.register(strategy, symbols={"BTC"})
        live, filled = asyncio.Event(), asyncio.Event()

        async def record(event: OrderEvent) -> None:
            if isinstance(event, OrderLive):
                live.set()
            if isinstance(event, OrderFilled):
                filled.set()

        bus.subscribe(OrderEvent, record)

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(live.wait(), timeout=5)
        # The first pass, while the order rests under its LIVE saga.
        clock.advance_to(31 * _NS)
        await _let_cadences_run()
        # The market crosses the resting BUY and the saga ends FILLED.
        clock.advance_to(40 * _NS)
        await bus.publish(
            MarketTick(
                ts_event=40 * _NS,
                ts_init=40 * _NS,
                symbol="BTC",
                price=Decimal("40000"),
                size=Decimal("3"),
                aggressor_side=AggressorSide.SELL,
                trade_id="cross",
                seq=1,
            )
        )
        await asyncio.wait_for(filled.wait(), timeout=5)
        # Two passes over the terminal saga.
        for t in (61, 91):
            clock.advance_to(t * _NS)
            await _let_cadences_run()
        assert engine.state is ComponentState.RUNNING
        await engine.stop()
        return engine, link, await run

    with capture_events() as events:
        engine, link, exit_code = asyncio.run(main())

    assert exit_code == 0
    resting = [VenueOpenOrder(symbol="BTC", cloid=_CLOID, venue_oid=None)]
    assert link.lists == [resting, [], []]
    noisy = {
        "reconcile.frozen",
        "reconcile.stray_cancelled",
        "reconcile.external_order",
        "order.place_answer_after_terminal",
    }
    assert [e["event"] for e in events if e["event"] in noisy] == []
