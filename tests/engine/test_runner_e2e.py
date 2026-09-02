"""Engine runner E2E (issue #19): the supervised lifecycle over the real pipeline.

The ``Engine`` host (ADR-0014/0024) wired with every real v1 concrete —
``ReplayFeed`` → ``StrategyHost``-hosted strategy → ``ExecutionManager`` →
``PaperExchange`` over ``InMemoryBus`` + ``SQLiteStore`` — driven through
``run()``: ordered startup gated on the reconciliation barrier, supervised
operation, and a graceful stop that snapshots strategies and closes the store.
Zero external services; the whole run is on ``ManualClock``.
"""

import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path

from kafka_fakes import FakeKafkaBroker
from ledgers import GENESIS
from structlog.typing import EventDict
from venue_doubles import (
    DERIVED_GENESIS,
    DERIVED_STATE,
    LIVE_ACCOUNT_ID,
    LiveVenueDouble,
    VenueDouble,
    account_state,
)

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.bus.kafka import KafkaBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.feed import ReplayFeed
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Account,
    ComponentState,
    Exchange,
    FillReport,
    InstrumentSpec,
    InvariantViolation,
    MarketTick,
    Order,
    OrderDenied,
    OrderEvent,
    OrderFilled,
    OrderLive,
    OrderState,
    OrderType,
    PlaceOrder,
    PlaceSignal,
    Portfolio,
    Side,
    TimeInForce,
    VenueAccountState,
    VenueOrderView,
    VenueReadFailure,
    derive_cloid,
)
from tickwright.engine.guard import RealGuard
from tickwright.engine.reconcile import ReconcileConfig
from tickwright.engine.runner import Engine, EngineConfig
from tickwright.observability.testing import capture_events
from tickwright.strategies import SingleShotLimitStrategy, SingleShotMarketStrategy

_ROWS: list[dict[str, str | int]] = [
    {
        "symbol": "BTC",
        "price": "42000",
        "size": "3",
        "aggressor_side": "buy",
        "trade_id": "a",
        "ts_event": 1_000,
    },
    {
        "symbol": "BTC",
        "price": "42100",
        "size": "3",
        "aggressor_side": "sell",
        "trade_id": "b",
        "ts_event": 2_000,
    },
]


def _write_ticks(path: Path) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in _ROWS) + "\n")
    return path


async def _run_to_fill_then_stop(
    ticks: Path, db: Path, *, instrument_specs: dict[str, InstrumentSpec] | None = None
) -> tuple[int, Engine, SingleShotMarketStrategy]:
    """One supervised life: full real wiring, run until the fill, stop gracefully.

    The strategy comes back with the engine because its ``Portfolio`` facade is
    the engine's own (#213) — what it read is the other end of what the engine
    wrote, and a caller asserting the pair needs both.

    ``instrument_specs`` is the venue's own universe, handed to the *exchange*
    and never to the engine: what a case passing it asserts is that the engine
    sourced it off the seam, exactly as it sources ``account_spec()``.
    """
    bus = InMemoryBus()
    clock = ManualClock()
    store = SQLiteStore(db)
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        instrument_specs=instrument_specs or {},
        account_net=dict,
    )
    feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
    engine = Engine(bus=bus, clock=clock, store=store, exchange=exchange, feed=feed)
    strategy = SingleShotMarketStrategy(
        strategy_id="trivial",
        bus=bus,
        clock=clock,
        portfolio=engine.portfolio_for("trivial"),
        side=Side.BUY,
        quantity=Decimal("0.5"),
    )
    engine.register(strategy, symbols={"BTC"})
    assert engine.state is ComponentState.READY

    filled = asyncio.Event()

    async def on_order_event(event: OrderEvent) -> None:
        if isinstance(event, OrderFilled):
            filled.set()

    bus.subscribe(OrderEvent, on_order_event)

    run = asyncio.create_task(engine.run())
    # The engine keeps running after replay end-of-file — like the CLI, it
    # stops only when told to (SIGTERM there, ``stop()`` here).
    await asyncio.wait_for(filled.wait(), timeout=5)
    assert engine.state is ComponentState.RUNNING
    await engine.stop()
    return await run, engine, strategy


def test_the_engine_lends_a_strategy_a_facade_onto_its_own_ledger(tmp_path: Path) -> None:
    """The registration loop's seam (#213): the engine owns the projection the
    way it owns the ``Cache``, and hands a strategy a scoped facade onto it — so
    the composition root never builds a second one to inject.

    Asserted on the number the *venue* was declared with rather than an
    arbitrary literal: what makes the facade the engine's own is that its cash
    line was opened from the same ``account_spec()`` the engine's exchange
    declares, which nothing here passed in separately.
    """
    bus = InMemoryBus()
    clock = ManualClock()
    engine = Engine(
        bus=bus,
        clock=clock,
        store=SQLiteStore(":memory:"),
        exchange=PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
        ),
        feed=ReplayFeed(path=_write_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock),
    )

    assert engine.portfolio_for("trivial").account().cash == GENESIS


def test_the_engine_values_a_position_against_the_venues_own_instrument_spec(
    tmp_path: Path,
) -> None:
    """The margin model's second venue-sourced input, wired the way the first is.

    Nothing here hands the engine a spec: the universe is declared on the
    ``PaperExchange`` and the engine sources it off ``instrument_specs()``, the
    exact peer of the ``account_spec()`` the case above pins. So a strategy
    reading its own ``Portfolio`` gets a maintenance figure computed against the
    venue's rate rather than the ``None`` an engine with no universe reports.

    ``margin_maint`` is the testnet-measured tier-0 rate the ADR-0041 §4.1
    amendment records (#152: 5873.49 notional against 73.418625 maintenance).
    The notional is the file's: the strategy fills 0.5 @ 42 000 and the last row
    marks at 42 100, so the leg is worth 0.5 × 42 100 = 21 050 and the venue's
    rate on it is 21 050 × 0.0125 = **263.125** — a figure derived from the
    replayed rows, not from the code's own arithmetic.
    """
    ticks = _write_ticks(tmp_path / "ticks.jsonl")
    spec = InstrumentSpec(
        symbol="BTC",
        sz_decimals=3,
        max_decimals=6,
        min_notional=Decimal("0"),
        margin_maint=Decimal("0.0125"),
    )

    _, engine, _ = asyncio.run(
        _run_to_fill_then_stop(ticks, tmp_path / "saga.db", instrument_specs={"BTC": spec})
    )

    view = engine.portfolio_for("trivial").position("BTC")
    assert view is not None
    assert view.notional == Decimal("21050")
    assert view.maintenance_margin == Decimal("263.125")
    # And the account line the strategy reads beside it totals the same rate
    # over the whole book, off the same universe.
    assert engine.portfolio_for("trivial").account().total_maintenance_margin == Decimal("263.125")


def test_run_replays_trades_and_exits_zero_on_graceful_stop(tmp_path: Path) -> None:
    ticks = _write_ticks(tmp_path / "ticks.jsonl")
    db = tmp_path / "saga.db"

    exit_code, engine, _ = asyncio.run(_run_to_fill_then_stop(ticks, db))

    assert exit_code == 0
    assert engine.state is ComponentState.STOPPED

    # The graceful stop closed the store; the durable trail survives the process:
    # the saga is FILLED and the final strategy snapshot was taken (ADR-0016).
    reopened = SQLiteStore(db)
    try:
        order = reopened.get_order(derive_cloid("trivial:BTC:1"))
        assert order is not None
        assert order.state is OrderState.FILLED
        assert order.cum_qty == Decimal("0.5")
        assert reopened.load_strategy_snapshot("trivial") is not None
    finally:
        reopened.close()


def test_a_restart_on_a_changed_genesis_refuses_before_a_tick_is_ever_replayed(
    tmp_path: Path,
) -> None:
    """The store refusal fires from the runner's own start sequence (ADR-0043 §10),
    and it is the **first** of the surface's three — ahead of both venue refusals.

    A full first life leaves a ledger behind. The second is wired to a paper venue
    declaring a different opening balance, which is the operator editing
    ``TICKWRIGHT_PAPER__GENESIS_COLLATERAL`` against a store that has already
    accrued away from the old one. It faults before the feed is ever started, so
    the run that must not trade places nothing — and the durable ledger it refused
    is exactly as the first life left it, the remedy (a fresh store path) still
    open.
    """
    ticks = _write_ticks(tmp_path / "ticks.jsonl")
    db = tmp_path / "saga.db"
    assert asyncio.run(_run_to_fill_then_stop(ticks, db))[0] == 0

    async def second_life() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        engine = Engine(
            bus=bus,
            clock=clock,
            store=SQLiteStore(db),
            exchange=PaperExchange(
                bus=bus,
                clock=clock,
                fill_model=ImmediateFillModel(),
                genesis_collateral=GENESIS * 2,
                account_net=dict,
            ),
            feed=ReplayFeed(path=ticks, bus=bus, clock=clock),
        )
        return await engine.run(), engine

    with capture_events() as logs:
        exit_code, engine = asyncio.run(second_life())

    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED
    # Faulted *for this reason*, not merely faulted: a start sequence has several
    # ways to fail, and only the trail says which one a restart hit.
    faults = [log for log in logs if log["event"] == "engine.faulted"]
    assert len(faults) == 1
    assert "StoreAccountMismatch" in faults[0]["error"]
    # And the feed never started — the refusal is ahead of every step that could
    # have put an order on the venue.
    assert [log for log in logs if log["event"] == "engine.feed_started"] == []

    reopened = SQLiteStore(db)
    try:
        row = reopened.load_account()
        assert row is not None
        assert row.genesis_collateral == GENESIS  # the first life's, unrewritten
    finally:
        reopened.close()


def test_a_strategy_reads_back_the_fill_the_engine_wrote(tmp_path: Path) -> None:
    """The facade and the engine's writer are one object, end to end (#213).

    Asserted on what crossed the seam rather than by identity: the strategy read
    ``position("BTC")`` inside its own ``on_order_event``, off the facade the
    engine lent it, and got the partition the engine's own projection had just
    folded that fill into. A facade bound to some other projection reads
    ``None`` here — the shape a strategy sees for a symbol it never traded.
    """
    _, _, strategy = asyncio.run(
        _run_to_fill_then_stop(_write_ticks(tmp_path / "ticks.jsonl"), tmp_path / "saga.db")
    )

    assert [(p.symbol, p.size) for p in strategy.positions if p is not None] == [
        ("BTC", Decimal("0.5"))
    ]


def test_the_engine_subscribes_the_mark_so_tier_two_is_readable_through_the_facade(
    tmp_path: Path,
) -> None:
    """The mark's ingress is the **runner's** wiring, not the projection's own.

    A ``MarkTick`` reaches the ledger by subscription — the one accounting input
    that does, funding aside — and nothing else in the engine subscribes on the
    projection's behalf. Without that line the whole Tier-2 surface reads
    ``None`` on a run whose feed is publishing marks every row, which is exactly
    the failure a strategy would misread as "no position worth anything".

    The strategy is long 0.5 from 42 000 and the file's last mark is 42 100, so
    the open leg is worth +50 — worked from the file, and readable through the
    facade the engine lent out.
    """
    _, engine, _ = asyncio.run(
        _run_to_fill_then_stop(_write_ticks(tmp_path / "ticks.jsonl"), tmp_path / "saga.db")
    )

    view = engine.portfolio_for("trivial").position("BTC")
    assert view is not None
    assert view.mark_ts == 2_000
    assert view.unrealized_pnl == Decimal("50")


def _flat_ticks(path: Path, *, prices: dict[str, str], rounds: int) -> Path:
    """``rounds`` passes over ``prices``, every symbol at one unchanging price.

    A frozen mark is what makes the margin figures below readable straight off
    the file: every fill lands at its symbol's price and every position carries
    zero unrealized PnL, so free margin is the cash line less the collateral the
    opens locked away, and nothing else moves it. The passes repeat because a
    strategy that waits for a *condition* rather than for its first tick needs
    the symbol quoted again after the condition arrives.
    """
    rows = [
        {
            "symbol": symbol,
            "price": price,
            "size": "10",
            "aggressor_side": "buy",
            "trade_id": f"{symbol}-{i}",
            "ts_event": 1_000 * (i * len(prices) + n + 1),
        }
        for i in range(rounds)
        for n, (symbol, price) in enumerate(prices.items())
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


class _MarginBlindStrategy:
    """Places its one order only once the account is *already* underwater.

    The reader ADR-0040 §7's "reported when negative, with no rejection and no
    liquidation" is a promise to. It refuses to trade while free margin is
    positive or unknown, so the order it does place is provably placed against a
    negative one — which an ordinary strategy firing on its first tick could
    never demonstrate, having traded before the account had anything to be
    underwater about. It trades a symbol of its own because free margin is an
    account-wide fact and same-symbol ownership is refused (ADR-0034): the
    breach it reads is one another strategy's position opened.
    """

    def __init__(
        self,
        *,
        bus: InMemoryBus,
        clock: ManualClock,
        portfolio: Portfolio,
        quantity: Decimal,
    ) -> None:
        self.strategy_id = "blind"
        self._bus = bus
        self._clock = clock
        self._portfolio = portfolio
        self._quantity = quantity
        self._seq = 1
        self.free_margin_at_placement: Decimal | None = None
        self.fills: list[OrderFilled] = []
        self.denials: list[OrderDenied] = []
        self.filled = asyncio.Event()

    async def on_tick(self, tick: MarketTick) -> None:
        if self.free_margin_at_placement is not None:
            return
        free = self._portfolio.account().free_margin
        if free is None or free >= 0:
            return
        self.free_margin_at_placement = free
        now = self._clock.timestamp_ns()
        await self._bus.publish(
            PlaceSignal(
                ts_event=now,
                ts_init=now,
                strategy_id=self.strategy_id,
                symbol=tick.symbol,
                seq=self._seq,
                side=Side.BUY,
                quantity=self._quantity,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.IOC,
                price=None,
                post_only=False,
            )
        )

    async def on_order_event(self, event: OrderEvent) -> None:
        if isinstance(event, OrderFilled):
            self.fills.append(event)
            self.filled.set()
        elif isinstance(event, OrderDenied):
            self.denials.append(event)

    def set_next_seq(self, next_seq: int) -> None:
        self._seq = next_seq

    def snapshot(self) -> bytes:
        return b""

    def restore(self, data: bytes) -> None:
        return None


def test_a_negative_free_margin_is_reported_and_stops_nothing(tmp_path: Path) -> None:
    """ADR-0040 §7's deliberate departure, asserted where it could actually bite.

    Paper reports a breach it does not enforce: the account goes underwater, and
    the run carries on placing, filling and holding exactly as it would have
    while solvent. That is the honest "you would have been rejected or
    liquidated on live" signal — a simulator that refused the order instead
    would report a solvency the operator does not have.

    Worked off the file and the venue's declared opening balance, never
    recomputed the way the code does. Both marks are frozen, so every position
    carries zero unrealized PnL and equity is the cash line: the overweight
    strategy's 3 BTC lock ``3 × 42 000 = 126 000`` of isolated collateral
    against a ``GENESIS`` of 100 000, leaving **−26 000**. The blind strategy's
    0.1 ETH then locks another ``0.1 × 2 000 = 200`` on top, for a final
    **−26 200**.

    The negative figure buys no consequence anywhere: the second order is filled
    rather than denied, both legs are still open at full size, and the store
    holds the two orders that were placed and no third one closing them out.
    """
    ticks = _flat_ticks(tmp_path / "ticks.jsonl", prices={"BTC": "42000", "ETH": "2000"}, rounds=4)
    db = tmp_path / "saga.db"

    async def one_life() -> tuple[int, Engine, _MarginBlindStrategy]:
        bus = InMemoryBus()
        clock = ManualClock()
        engine = Engine(
            bus=bus,
            clock=clock,
            store=SQLiteStore(db),
            exchange=PaperExchange(
                bus=bus,
                clock=clock,
                fill_model=ImmediateFillModel(),
                genesis_collateral=GENESIS,
                account_net=dict,
            ),
            feed=ReplayFeed(path=ticks, bus=bus, clock=clock),
        )
        overweight = SingleShotMarketStrategy(
            strategy_id="overweight",
            bus=bus,
            clock=clock,
            portfolio=engine.portfolio_for("overweight"),
            side=Side.BUY,
            quantity=Decimal("3"),
        )
        blind = _MarginBlindStrategy(
            bus=bus,
            clock=clock,
            portfolio=engine.portfolio_for("blind"),
            quantity=Decimal("0.1"),
        )
        engine.register(overweight, symbols={"BTC"})
        engine.register(blind, symbols={"ETH"})

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(blind.filled.wait(), timeout=5)
        await engine.stop()
        return await run, engine, blind

    exit_code, engine, blind = asyncio.run(one_life())

    assert exit_code == 0
    # It traded *because* the account was underwater, by the file's own numbers.
    assert blind.free_margin_at_placement == Decimal("-26000")
    # And the breach denied it nothing: one fill, no denial.
    assert [f.quantity for f in blind.fills] == [Decimal("0.1")]
    assert blind.denials == []
    # The account is deeper underwater afterwards and says so plainly.
    assert engine.portfolio_for("blind").account().free_margin == Decimal("-26200")
    # Nothing was liquidated: both legs are still open at the size they opened.
    overweight_view = engine.portfolio_for("overweight").position("BTC")
    blind_view = engine.portfolio_for("blind").position("ETH")
    assert overweight_view is not None and overweight_view.size == Decimal("3")
    assert blind_view is not None and blind_view.size == Decimal("0.1")

    # No closing order was ever written either — the durable trail holds the two
    # orders the strategies placed and nothing the engine placed on their behalf.
    reopened = SQLiteStore(db)
    try:
        orders = reopened.all_orders()
        assert {(o.strategy_id, o.symbol, o.side, o.state) for o in orders} == {
            ("overweight", "BTC", Side.BUY, OrderState.FILLED),
            ("blind", "ETH", Side.BUY, OrderState.FILLED),
        }
        assert len(orders) == 2
    finally:
        reopened.close()


_LIVE_CLOID = "0xlive"
_NO_RECORD = VenueOrderView(status=None)


class _LiveShapedVenue(LiveVenueDouble):
    """A live-shaped venue that also answers the barrier's order reads.

    The account half — the ingested-genesis declaration and the account read
    itself — is the shared ceremony. What this adds is ``view``: what the
    mass-rebuild finds for a surviving saga, so a case can put a fill *inside*
    the barrier, which is where the ordering against the materialisation is
    decided.
    """

    def __init__(
        self,
        *,
        state: VenueAccountState | None = DERIVED_STATE,
        view: VenueOrderView | VenueReadFailure = _NO_RECORD,
    ) -> None:
        super().__init__(state=state)
        self._view = view

    async def place(self, order: PlaceOrder) -> None:
        raise AssertionError("nothing is placed: no strategy is registered")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("nothing is cancelled: no order exists")

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        return self._view


def _live_run(
    tmp_path: Path,
    store: SQLiteStore,
    venue: Exchange,
    *,
    opening_cash: Decimal = Decimal("0"),
) -> int:
    """One supervised life of a strategy-less engine over ``venue``.

    ``opening_cash`` is what the ledger reads *before* the barrier: zero on
    live, where ``Account.open`` resolves a ``None`` genesis and the real number
    has not been read yet, and the declared genesis on paper, where it has been
    in hand since composition.
    """

    async def one_life() -> int:
        bus = InMemoryBus()
        clock = ManualClock()
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=venue,
            feed=ReplayFeed(path=_write_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock),
        )
        # Never ``None``, which ADR-0041 §6 forbids — and on live never a number
        # anybody chose either: the zero ``Account.open`` resolves is what an
        # *unstarted* live ledger reports, not a collateral default.
        assert engine.portfolio_for("live").account().cash == opening_cash
        run = asyncio.create_task(engine.run())
        await engine.stop()
        return await run

    return asyncio.run(one_life())


def test_a_live_first_start_materialises_its_account_row_at_the_barrier(
    tmp_path: Path,
) -> None:
    """A live run's account row exists before any strategy starts (ADR-0043 §6).

    Paper declares its genesis and live derives one; the two reach the same
    populated row by different routes, and neither ever starts against a row
    that does not exist. 25.9604 is the recorded cross snapshot's own arithmetic
    — ``accountValue`` 25.9264 net of an unrealized −0.034 — not this engine's
    restated.
    """
    store = SQLiteStore(tmp_path / "saga.db")
    venue = _LiveShapedVenue()

    assert _live_run(tmp_path, store, venue) == 0

    assert venue.account_reads == 1  # read once, to create the row it lacked
    reopened = SQLiteStore(tmp_path / "saga.db")
    try:
        row = reopened.load_account()
        assert row is not None
        assert row.account_id == LIVE_ACCOUNT_ID
        assert row.genesis_collateral == DERIVED_GENESIS
        assert row.cash == DERIVED_GENESIS
    finally:
        reopened.close()


class _AccountReadingStrategy:
    """A strategy that does nothing but read its cash line on the first tick —
    the reader ADR-0041 §6's "``cash`` is Tier-1, never ``None``" is a promise to."""

    def __init__(self, portfolio: Portfolio) -> None:
        self.strategy_id = "live"
        self._portfolio = portfolio
        self.first_read: Decimal | None = None
        self.read = asyncio.Event()

    async def on_tick(self, tick: MarketTick) -> None:
        if self.first_read is None:
            self.first_read = self._portfolio.account().cash
            self.read.set()

    async def on_order_event(self, event: OrderEvent) -> None:
        return None

    def set_next_seq(self, next_seq: int) -> None:
        return None

    def snapshot(self) -> bytes:
        return b""

    def restore(self, data: bytes) -> None:
        return None


def test_a_strategys_first_account_read_on_a_live_start_is_the_derived_one(
    tmp_path: Path,
) -> None:
    """The whole point of materialising at the *barrier* (ADR-0043 §6).

    Strategies start one step behind the barrier and the feed one behind them,
    so the earliest a strategy can read anything is already past the
    materialisation — which is why a first reader sees the venue's number and
    never the zero an unstarted live ledger reports, and never a missing
    account. Put the same write on a cadence instead and this read is the one
    that breaks.
    """

    async def one_life() -> Decimal | None:
        bus = InMemoryBus()
        clock = ManualClock()
        engine = Engine(
            bus=bus,
            clock=clock,
            store=SQLiteStore(tmp_path / "saga.db"),
            exchange=_LiveShapedVenue(),
            feed=ReplayFeed(path=_write_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock),
        )
        strategy = _AccountReadingStrategy(engine.portfolio_for("live"))
        engine.register(strategy, symbols={"BTC"})

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(strategy.read.wait(), timeout=5)
        await engine.stop()
        assert await run == 0
        return strategy.first_read

    assert asyncio.run(one_life()) == DERIVED_GENESIS


def test_the_derived_genesis_is_named_in_the_trail(tmp_path: Path) -> None:
    """Every component state change emits a named event (ADR-0020), and this one
    earns it more than most: the number is derived once, from a venue response
    nobody kept, and then stands for the life of the ledger. Nothing
    cross-checks it afterwards — live genesis is provenance only — so the record
    of *what was read* is the only account an operator will ever get of where
    the opening balance came from.
    """
    store = SQLiteStore(tmp_path / "saga.db")

    with capture_events() as logs:
        assert _live_run(tmp_path, store, _LiveShapedVenue()) == 0

    materialised = [log for log in logs if log["event"] == "account.materialised"]
    assert len(materialised) == 1
    assert materialised[0]["account_id"] == LIVE_ACCOUNT_ID
    assert materialised[0]["genesis_collateral"] == "25.9604"
    assert materialised[0]["run_id"]  # inside the run's correlation (ADR-0020)


class _PaperShapedVenue(VenueDouble):
    """The paper declaration, over a venue account read that must never happen."""

    async def fetch_account_state(self) -> VenueAccountState | None:
        raise AssertionError("paper has no account truth: the barrier must not ask for one")

    async def place(self, order: PlaceOrder) -> None:
        raise AssertionError("nothing is placed: no strategy is registered")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("nothing is cancelled: no order exists")

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        return _NO_RECORD


def test_a_paper_start_performs_no_venue_account_read_at_the_barrier(
    tmp_path: Path,
) -> None:
    """The materialisation is live-only, and what makes it so is the row rather
    than a venue-kind flag (ADR-0043 §6): paper's genesis was seeded three steps
    earlier inside ``recover()``, from a config value already in hand, so by the
    time the barrier runs there is nothing left to create.

    Asserted as a refusal rather than a call count: ``PaperExchange`` answers
    ``None`` to this read by construction — the fail-closed value — so a
    materialisation that asked anyway would freeze the barrier and fault the
    default path on a venue that has nothing to say. The double raises instead,
    which is the same mistake made loud.
    """
    store = SQLiteStore(tmp_path / "saga.db")

    assert _live_run(tmp_path, store, _PaperShapedVenue(), opening_cash=GENESIS) == 0

    reopened = SQLiteStore(tmp_path / "saga.db")
    try:
        row = reopened.load_account()
        assert row is not None
        assert row.account_id == "paper-default"
        assert row.genesis_collateral == GENESIS
    finally:
        reopened.close()


def test_a_live_restart_neither_re_derives_nor_overwrites_the_recorded_genesis(
    tmp_path: Path,
) -> None:
    """Genesis is written once (ADR-0042 §3), and on live nothing could ever
    catch a second write: the value is *provenance only* — there is no
    configured counterpart to check it against, so #188's genesis refusal is
    paper-only by ADR-0043 §10's predicate.

    The second life is handed a venue whose equity has moved (a deposit, or the
    same position marked differently), which is exactly the legitimate change
    that must **not** rewrite the opening declaration. The read is skipped
    outright rather than made and discarded: the row's existence is the
    predicate, so a restart owes the venue nothing at this step.
    """
    db = tmp_path / "saga.db"
    assert _live_run(tmp_path, SQLiteStore(db), _LiveShapedVenue()) == 0

    # A second life over the store the first one left behind — its graceful stop
    # closed the handle, exactly as a restart would.
    moved = _LiveShapedVenue(state=account_state("99999", "-0.034"))
    assert _live_run(tmp_path, SQLiteStore(db), moved) == 0

    assert moved.account_reads == 0
    reopened = SQLiteStore(tmp_path / "saga.db")
    try:
        row = reopened.load_account()
        assert row is not None
        assert row.genesis_collateral == DERIVED_GENESIS  # the first life's
        assert row.cash == DERIVED_GENESIS
    finally:
        reopened.close()


def test_an_account_read_that_never_answers_faults_rather_than_clearing(
    tmp_path: Path,
) -> None:
    """The barrier is never cleared on an assumed-flat account (ADR-0043 §6).

    A ``clearinghouseState`` that will not answer exhausts the same
    ``startup_reconciliation_timeout`` budget the mass-rebuild spends and then
    faults, for the supervisor to backoff-restart. The alternative — starting
    anyway — is the one outcome this step exists to prevent: strategies running
    against a ledger with no account row at all, which is also the fabricated
    flat ADR-0034 forbids.
    """
    store = SQLiteStore(tmp_path / "saga.db")
    venue = _LiveShapedVenue(state=None)

    assert _live_run(tmp_path, store, venue) == 1  # FAULTED → non-zero exit

    assert venue.account_reads > 1, "the budget must be retried, not spent on one read"
    reopened = SQLiteStore(tmp_path / "saga.db")
    try:
        assert reopened.load_account() is None  # nothing guessed in the meantime
    finally:
        reopened.close()


def test_a_barrier_fill_lands_on_the_materialised_row_not_a_zero_one(
    tmp_path: Path,
) -> None:
    """The materialisation is ordered *before* the mass-rebuild, not merely
    inside the same barrier (ADR-0043 §6).

    The rebuild emits synthetic fills; every fill routes through the atomic
    ledger write, and that write carries the account row because ADR-0043 §9
    makes ``account`` required — every mutation moves cash. So a rebuild that
    ran first would **create** the live row itself, at the zero ``Account.open``
    resolves a ``None`` genesis to; the materialisation behind it would decline
    to overwrite a non-``None`` row, and that zero would stand for the life of
    the ledger with nothing to refuse it — #188's genesis comparison is
    paper-only by ADR-0043 §10's predicate.

    The fill is an opening one at zero fees, so it realizes nothing: the cash
    line is the derived genesis either way, and only ``genesis_collateral``
    tells the two orderings apart.
    """
    store = SQLiteStore(tmp_path / "saga.db")
    store.checkpoint(_submitted_saga(_LIVE_CLOID), ts_ns=500)
    venue = _LiveShapedVenue(
        view=VenueOrderView(
            status=None,
            fills=(
                FillReport(
                    ts_event=900,
                    ts_init=900,
                    cloid=_LIVE_CLOID,
                    symbol="BTC",
                    trade_id="t-1",
                    quantity=Decimal("0.002"),
                    price=Decimal("64809"),
                ),
            ),
        )
    )

    assert _live_run(tmp_path, store, venue) == 0

    reopened = SQLiteStore(tmp_path / "saga.db")
    try:
        healed = reopened.get_order(_LIVE_CLOID)
        assert healed is not None
        assert healed.cum_qty == Decimal("0.002"), "the barrier must reconcile a fill to bite"
        row = reopened.load_account()
        assert row is not None
        assert row.genesis_collateral == DERIVED_GENESIS  # derived, never the fill's zero
        assert row.cash == DERIVED_GENESIS
    finally:
        reopened.close()


_ACCOUNT_INTERVAL = 60.0
_ACCOUNT_INTERVAL_NS = int(_ACCOUNT_INTERVAL * 1_000_000_000)


class _CadenceWatchingLiveVenue(_LiveShapedVenue):
    """A live venue that signals the account read the *cadence* makes.

    A first start already reads once, at the barrier, so "the cadence ran" is
    the **second** read rather than merely a non-zero count.
    """

    def __init__(self) -> None:
        super().__init__()
        self.cycled = asyncio.Event()

    async def fetch_account_state(self) -> VenueAccountState | None:
        state = await super().fetch_account_state()
        if self.account_reads > 1:
            self.cycled.set()
        return state


def _one_life_past_the_account_deadline(
    tmp_path: Path, venue: Exchange, *, settle: Callable[[Engine], Awaitable[None]]
) -> tuple[int, ComponentState]:
    """One supervised life of a strategy-less engine, driven across an
    account-cadence deadline on the virtual clock.

    The feed **blocks** rather than replaying, so the only thing moving virtual
    time is this helper: a cycle that fires here fired because the runner
    scheduled it, not because a replayed tick swept the deadline on its way
    past. ``settle`` is how each case waits for its own outcome — the cadence
    reaching the venue, or a stretch of loop slots proving it never will.
    """

    async def one_life() -> tuple[int, ComponentState]:
        bus = InMemoryBus()
        clock = ManualClock()
        feed = _BlockingFeed()
        engine = Engine(
            bus=bus,
            clock=clock,
            store=SQLiteStore(tmp_path / "saga.db"),
            exchange=venue,
            feed=feed,
            config=EngineConfig(
                reconcile=ReconcileConfig(account_interval_seconds=_ACCOUNT_INTERVAL)
            ),
        )
        run = asyncio.create_task(engine.run())
        # The cadences are created ahead of the feed in the same TaskGroup, so
        # a started feed proves every cadence is already parked on its deadline
        # — advancing before that could cross a deadline nobody was waiting on.
        await asyncio.wait_for(feed.started.wait(), timeout=5)
        clock.advance_to(_ACCOUNT_INTERVAL_NS)
        await settle(engine)
        await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5), engine.state

    return asyncio.run(one_life())


def test_a_live_run_reconciles_the_account_on_its_own_cadence(tmp_path: Path) -> None:
    """The account grain gets a cadence of its own on live (ADR-0034).

    Anchored on one ``fetch_account_state`` read per cycle, which is also the
    only seam the wiring is observable through: the cadence tasks are the
    runner's, so what a case can see is the venue being read a second time,
    at a deadline nothing but the cadence was waiting on.
    """
    venue = _CadenceWatchingLiveVenue()

    async def settle(engine: Engine) -> None:
        await asyncio.wait_for(venue.cycled.wait(), timeout=5)

    assert _one_life_past_the_account_deadline(tmp_path, venue, settle=settle) == (
        0,
        ComponentState.STOPPED,
    )
    assert venue.account_reads == 2, "the barrier's read, then exactly one cycle's"


def test_no_paper_run_schedules_the_account_cadence(tmp_path: Path) -> None:
    """Paper has no second account to reconcile against, so nothing runs there
    (ADR-0034, ADR-0043 §4) — its atomic ledger write stands in.

    Asserted as a refusal rather than a call count, for the reason the barrier's
    sibling case gives: ``PaperExchange`` answers ``None`` to this read by
    construction, the fail-closed value, so a cadence scheduled here would
    freeze every cycle against a venue that has nothing to say and look like an
    outage. The double raises instead, which faults the run — the same mistake
    made loud, and a clean exit is the whole assertion.
    """
    venue = _PaperShapedVenue()

    async def settle(engine: Engine) -> None:
        for _ in range(5):  # every slot a scheduled cycle would need to reach the venue
            await asyncio.sleep(0)

    assert _one_life_past_the_account_deadline(tmp_path, venue, settle=settle) == (
        0,
        ComponentState.STOPPED,
    )


def test_a_fill_leaves_the_order_row_and_the_ledger_in_the_one_store(tmp_path: Path) -> None:
    """The atomic write (ADR-0043 §4), proved on a fully-wired engine: both
    halves are read back from the single store the engine was handed.

    Structural rather than conventional since #213 — the engine opens its ledger
    over that same store, so there is no second store the ledger could have gone
    to and no wiring a caller could get wrong. The read is deliberately from a
    *reopened* store: what the fill made durable, not what a live handle caches.
    """
    db = tmp_path / "saga.db"

    asyncio.run(_run_to_fill_then_stop(_write_ticks(tmp_path / "ticks.jsonl"), db))

    reopened = SQLiteStore(db)
    try:
        order = reopened.get_order(derive_cloid("trivial:BTC:1"))
        assert order is not None
        assert order.state is OrderState.FILLED
        positions = reopened.all_positions()
        assert [(p.strategy_id, p.symbol, p.signed_size) for p in positions] == [
            ("trivial", "BTC", Decimal("0.5"))
        ]
        # The account row rode the same transaction (ADR-0043 §9) — still at
        # genesis, an opening fill at zero fees realizing nothing to move it.
        account = reopened.load_account()
        assert account is not None
        assert account.cash == GENESIS
    finally:
        reopened.close()


def test_named_events_prove_the_startup_order(tmp_path: Path) -> None:
    """ADR-0024's ordering, observable: the barrier clears before the feed
    starts, and nothing places before the barrier — inv 5 of ADR-0011."""
    ticks = _write_ticks(tmp_path / "ticks.jsonl")

    with capture_events() as logs:
        asyncio.run(_run_to_fill_then_stop(ticks, tmp_path / "saga.db"))

    names = [log["event"] for log in logs]
    barrier_at = names.index("engine.barrier_cleared")
    feed_at = names.index("engine.feed_started")
    assert barrier_at < feed_at
    placed_at = [i for i, name in enumerate(names) if name == "order.placed"]
    assert placed_at, "the run must actually place an order for the proof to bite"
    assert all(barrier_at < i for i in placed_at)

    # The runner owns the run-id correlation binding: every record after the
    # bind — the whole lifecycle — is traceable to this run (ADR-0020).
    assert _every_lifecycle_record_carries_a_run_id(logs)


def _every_lifecycle_record_carries_a_run_id(logs: list[EventDict]) -> bool:
    engine_records = [log for log in logs if str(log["event"]).startswith("engine.")]
    return bool(engine_records) and all(log.get("run_id") for log in engine_records)


async def _until(condition: Callable[[], bool]) -> None:
    """Spin the loop (bounded by the caller's ``wait_for``) until ``condition``
    holds — signal delivery is genuinely asynchronous, so the test waits for
    the observable effect rather than assuming a delivery order."""
    while not condition():
        await asyncio.sleep(0)


def test_sigterm_stops_the_engine_gracefully(tmp_path: Path) -> None:
    """The operator contract (ADR-0024): SIGTERM → graceful stop → exit 0."""
    ticks = _write_ticks(tmp_path / "ticks.jsonl")

    async def main() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        exchange = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
        )
        feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
        engine = Engine(bus=bus, clock=clock, store=store, exchange=exchange, feed=feed)

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(lambda: engine.state is ComponentState.RUNNING), timeout=5)
        os.kill(os.getpid(), signal.SIGTERM)
        return await asyncio.wait_for(run, timeout=5), engine

    exit_code, engine = asyncio.run(main())

    assert exit_code == 0
    assert engine.state is ComponentState.STOPPED


def test_sigusr1_trips_the_kill_switch_and_sigusr2_resets_it(tmp_path: Path) -> None:
    """The operator kill switch (ADR-0026): SIGUSR1 halts placements durably —
    subsequent ones are DENIED through the real guard — and SIGUSR2 re-enables."""
    spec = InstrumentSpec(
        symbol="BTC", sz_decimals=3, max_decimals=6, max_sig_figs=5, min_notional=Decimal("10")
    )

    def limit_signal(seq: int) -> PlaceSignal:
        return PlaceSignal(
            ts_event=1_000,
            ts_init=1_000,
            strategy_id="operator",
            symbol="BTC",
            seq=seq,
            side=Side.BUY,
            quantity=Decimal("0.5"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            price=Decimal("41000"),
        )

    async def main() -> None:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        venue = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
        )
        feed = ReplayFeed(path=_write_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock)
        guard = RealGuard(specs={"BTC": spec}, store=store, clock=clock)
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=venue,
            feed=feed,
            guard=guard,
        )

        outcomes: list[OrderEvent] = []
        ticks_seen = asyncio.Event()

        async def record(event: OrderEvent) -> None:
            outcomes.append(event)

        async def on_tick(_: MarketTick) -> None:
            ticks_seen.set()

        bus.subscribe(OrderEvent, record)
        bus.subscribe(MarketTick, on_tick)

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(lambda: engine.state is ComponentState.RUNNING), timeout=5)
        # The venue prices limits off the latest tick: wait for the replay to land.
        await asyncio.wait_for(ticks_seen.wait(), timeout=5)

        os.kill(os.getpid(), signal.SIGUSR1)
        await asyncio.wait_for(_until(lambda: guard.kill_switch_tripped), timeout=5)
        await bus.publish(limit_signal(seq=1))
        assert isinstance(outcomes[-1], OrderDenied)

        os.kill(os.getpid(), signal.SIGUSR2)
        await asyncio.wait_for(_until(lambda: not guard.kill_switch_tripped), timeout=5)
        await bus.publish(limit_signal(seq=2))
        assert isinstance(outcomes[-1], OrderLive)

        await engine.stop()
        assert await run == 0

    asyncio.run(main())


class _HangingFeed:
    """A feed whose ``stop()`` never returns — a wedged venue connection at
    teardown. A ``MarketFeed`` double at the venue boundary."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await asyncio.Event().wait()


class _BlockingFeed:
    """A feed whose read loop runs until cancelled — a live feed that never
    reaches end-of-file, unlike a replay. A ``MarketFeed`` double at the venue
    boundary; ``started`` fires once the loop is actually running."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def start(self) -> None:
        self.started.set()
        await asyncio.Event().wait()

    async def stop(self) -> None:
        return None


class _RecoveryOrderStore(SQLiteStore):
    """The real store, recording the two recovery reads whose order is the
    contract: the ledger's ``load_account`` and the ``Cache``'s ``all_orders``.

    The ordering has no other observation port. Both steps are the runner's own
    and neither leaves a distinguishing durable trace, so the seam they share is
    where it shows — recorded, not simulated: every call still reaches the real
    store underneath."""

    def __init__(self, path: Path, timeline: list[str]) -> None:
        super().__init__(path)
        self._timeline = timeline

    def load_account(self) -> Account | None:
        self._timeline.append("ledger.load_account")
        return super().load_account()

    def all_orders(self) -> list[Order]:
        self._timeline.append("cache.all_orders")
        return super().all_orders()


def test_the_ledger_is_recovered_before_the_order_cache_is_rebuilt(tmp_path: Path) -> None:
    """``PortfolioProjection.recover()`` runs immediately after the run-id bind
    and **before** ``cache.rebuild()`` (ADR-0043 §6/§10).

    The order is load-bearing rather than tidy: recovery's first step asks for
    ``load_account`` and the partitions behind it, where the rebuild deserializes
    every saga in the store — partitions are bounded by strategy × symbol, sagas
    by all the history the store holds. Behind the rebuild, a restart that must
    not trade at all would pay that mass read before finding out; the refusal
    that makes such a restart possible is the ledger step's own first act, and it
    asks ``has_orders()`` rather than the mass read precisely so a refused store
    costs one existence question.
    """
    timeline: list[str] = []

    async def main() -> int:
        store = _RecoveryOrderStore(tmp_path / "saga.db", timeline)
        bus = InMemoryBus()
        clock = ManualClock()
        feed = _BlockingFeed()
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=PaperExchange(
                bus=bus,
                clock=clock,
                fill_model=ImmediateFillModel(),
                genesis_collateral=GENESIS,
                account_net=dict,
            ),
            feed=feed,
        )
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(feed.started.wait(), timeout=5)
        await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5)

    assert asyncio.run(main()) == 0

    assert timeline[:2] == ["ledger.load_account", "cache.all_orders"]


def test_shutdown_is_bounded_a_hung_teardown_faults_instead_of_hanging(tmp_path: Path) -> None:
    """ADR-0024: the reverse shutdown is bounded by ``shutdown_timeout`` — a
    teardown that cannot finish must fault non-zero, never wedge the process."""

    async def main() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        exchange = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
        )
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=exchange,
            feed=_HangingFeed(),
            config=EngineConfig(shutdown_timeout_seconds=0.05),
        )
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(lambda: engine.state is ComponentState.RUNNING), timeout=5)
        await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5), engine

    exit_code, engine = asyncio.run(main())

    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED


def test_graceful_stop_cancels_a_still_running_feed(tmp_path: Path) -> None:
    """ADR-0024 reverse shutdown: a graceful stop of a feed still mid-read
    cancels the feed task and exits 0 — the live-feed path that a replay hitting
    end-of-file never exercises. The feed's ``start()`` never returns, so exit 0
    is only reachable if the reverse shutdown cancelled the task; otherwise the
    ``TaskGroup`` would wait on it forever and the run would time out."""

    async def main() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        exchange = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
        )
        feed = _BlockingFeed()
        engine = Engine(bus=bus, clock=clock, store=store, exchange=exchange, feed=feed)

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(lambda: engine.state is ComponentState.RUNNING), timeout=5)
        await asyncio.wait_for(feed.started.wait(), timeout=5)
        await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5), engine

    exit_code, engine = asyncio.run(main())

    assert exit_code == 0
    assert engine.state is ComponentState.STOPPED


def test_invariant_violation_faults_the_engine_and_exits_nonzero(tmp_path: Path) -> None:
    """ADR-0014/0024 fail-fast: an ``InvariantViolation`` in an engine-internal
    handler pierces everything — siblings cancelled, ``FAULTED``, non-zero exit."""
    ticks = _write_ticks(tmp_path / "ticks.jsonl")

    async def faulted_life() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        exchange = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
        )
        feed = ReplayFeed(path=ticks, bus=bus, clock=clock)
        # A real guard with no specs: the first placement is a composition-root
        # wiring bug (ADR-0031) and raises InvariantViolation inside the raw
        # ExecutionManager.on_signal handler — the engine must not contain it.
        guard = RealGuard(specs={}, store=store, clock=clock)
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=exchange,
            feed=feed,
            guard=guard,
        )
        engine.register(
            SingleShotMarketStrategy(
                strategy_id="doomed",
                bus=bus,
                clock=clock,
                portfolio=engine.portfolio_for("doomed"),
                side=Side.BUY,
                quantity=Decimal("0.5"),
            ),
            symbols={"BTC"},
        )
        return await engine.run(), engine

    with capture_events() as logs:
        exit_code, engine = asyncio.run(faulted_life())

    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED
    assert "engine.faulted" in [log["event"] for log in logs]


def test_a_broken_stop_hook_on_the_fault_path_is_recorded_not_swallowed(tmp_path: Path) -> None:
    """ADR-0020/0024: the faulted teardown is best-effort but not silent. A stop
    hook that raises mid-fault (here the store failing to close) is recorded as
    ``engine.stop_hook_failed`` and cannot mask the fault or block the non-zero
    exit — the operator sees the lost resource in the same run's trail."""

    class _StoreThatBreaksOnClose(SQLiteStore):
        def close(self) -> None:
            raise RuntimeError("store close broke during fault teardown")

    async def faulted_life() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        store = _StoreThatBreaksOnClose(tmp_path / "saga.db")
        exchange = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
        )
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=exchange,
            feed=_FaultingFeed(),
        )
        return await engine.run(), engine

    with capture_events() as logs:
        exit_code, engine = asyncio.run(faulted_life())

    names = [log["event"] for log in logs]
    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED
    # The fault is not masked by the broken hook, and the break is on the record.
    assert "engine.faulted" in names
    hook_failures = [log for log in logs if log["event"] == "engine.stop_hook_failed"]
    assert len(hook_failures) == 1
    assert hook_failures[0]["hook"] == "store.close"


class _FaultingFeed:
    """A feed whose read loop breaks an engine assumption — the fail-fast class
    — recording the teardown that still cuts it, for the suites that ask where
    in the sequence that happened. A ``MarketFeed`` double at the venue
    boundary."""

    def __init__(self, timeline: list[str] | None = None) -> None:
        self._timeline = timeline if timeline is not None else []

    async def start(self) -> None:
        raise InvariantViolation("the read loop broke an engine assumption")

    async def stop(self) -> None:
        self._timeline.append("feed.stop")


def _kafka_bus(broker: FakeKafkaBroker) -> KafkaBus:
    return KafkaBus(
        bootstrap_servers="kafka:9092",
        topic="tickwright.events",
        group_id="tickwright",
        producer_factory=broker.producer,
        consumer_factory=broker.consumer,
    )


def test_the_runner_owns_the_bus_lifecycle_connect_on_start_disconnect_on_stop(
    tmp_path: Path,
) -> None:
    """ADR-0024 steps 3 and the reverse shutdown: the runner starts the bus
    (Kafka: connect producer/consumer) and closes it on a graceful stop —
    observed at the process boundary, where the broker sees its clients."""
    broker = FakeKafkaBroker()

    async def main() -> tuple[int, Engine]:
        bus = _kafka_bus(broker)
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        exchange = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
        )
        engine = Engine(
            bus=bus,
            clock=clock,
            store=store,
            exchange=exchange,
            feed=_BlockingFeed(),
        )

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(lambda: engine.state is ComponentState.RUNNING), timeout=5)
        # Mid-run the bus is connected: the broker handed out started clients.
        assert [p.started for p in broker.producers] == [True]
        assert [c.started for c in broker.consumers] == [True]
        await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5), engine

    exit_code, engine = asyncio.run(main())

    assert exit_code == 0
    assert engine.state is ComponentState.STOPPED
    # The reverse shutdown closed the bus: every client disconnected.
    assert [p.started for p in broker.producers] == [False]
    assert [c.started for c in broker.consumers] == [False]


def test_the_fault_path_walks_the_same_teardown_feed_stopped_and_bus_closed(
    tmp_path: Path,
) -> None:
    """The faulted teardown shares membership and order with the graceful one,
    differing only in failure policy (ADR-0024): a fault must still stop the
    feed (a live WS must not leak) and close the bus (a Kafka producer must
    flush — buffered writes survive the fault)."""
    broker = FakeKafkaBroker()
    timeline: list[str] = []
    feed = _FaultingFeed(timeline)

    async def faulted_life() -> tuple[int, Engine]:
        bus = _kafka_bus(broker)
        clock = ManualClock()
        store = SQLiteStore(tmp_path / "saga.db")
        exchange = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
        )
        engine = Engine(bus=bus, clock=clock, store=store, exchange=exchange, feed=feed)
        return await engine.run(), engine

    exit_code, engine = asyncio.run(faulted_life())

    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED
    assert timeline == ["feed.stop"], "the fault path must stop the feed, not just cancel its task"
    assert [p.started for p in broker.producers] == [False]
    assert [c.started for c in broker.consumers] == [False]


class _LifecycleRecordingVenue(VenueDouble):
    """An ``Exchange`` that records the runner driving its lifecycle verbs, and
    what the rest of the process had already done by then. A network boundary is
    the one place a test double is allowed."""

    def __init__(self, timeline: list[str], broker: FakeKafkaBroker | None = None) -> None:
        self._timeline = timeline
        self._broker = broker
        self.bus_connected_at_start = False

    async def start(self) -> None:
        if self._broker is not None:
            # Observed at the process boundary: the broker has handed out
            # started clients, so the bus this venue reports on is already up.
            self.bus_connected_at_start = bool(self._broker.producers) and all(
                producer.started for producer in self._broker.producers
            )
        self._timeline.append("exchange.start")

    async def stop(self) -> None:
        self._timeline.append("exchange.stop")

    async def place(self, order: PlaceOrder) -> None:
        # Recorded rather than refused: a refusal here would fault the engine
        # through the saga path, which is not what these tests are asking about.
        self._timeline.append("exchange.place")

    async def cancel(self, cloid: str) -> None:
        self._timeline.append("exchange.cancel")

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        self._timeline.append("venue.read")
        return VenueOrderView(status=None)


def _submitted_saga(cloid: str) -> Order:
    """A saga the barrier must ask the venue about — without one, the mass
    rebuild has nothing to read and the ordering proof has no venue read to
    stand on."""
    order = Order(
        cloid=cloid,
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.LIMIT,
    )
    order.state = OrderState.SUBMITTED
    return order


def test_the_runner_starts_the_exchange_after_the_bus_and_before_the_barrier(
    tmp_path: Path,
) -> None:
    """ADR-0024 step 4: the ``Exchange``'s connect half runs after the bus is up
    — nothing can publish before that — and *before* the startup barrier, so a
    venue refusal precedes any order and the barrier observes an aligned venue."""
    broker = FakeKafkaBroker()
    timeline: list[str] = []
    venue = _LifecycleRecordingVenue(timeline, broker)

    async def main() -> int:
        store = SQLiteStore(tmp_path / "saga.db")
        # The ledger the prior life opened, beside the saga it left in flight:
        # order history with no account row behind it is the one shape #188
        # refuses outright (ADR-0043 §8), and this case is about lifecycle
        # ordering rather than about a store no engine may start on.
        store.checkpoint_ledger(
            account=Account.restore(
                account_id="paper-default",
                genesis_collateral=GENESIS,
                genesis_ts_ns=400,
                cash=GENESIS,
            ),
            ts_ns=400,
        )
        store.checkpoint(_submitted_saga("0xabc"), ts_ns=500)
        engine = Engine(
            bus=_kafka_bus(broker),
            clock=ManualClock(),
            store=store,
            exchange=venue,
            feed=_BlockingFeed(),
        )
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(_until(lambda: engine.state is ComponentState.RUNNING), timeout=5)
        await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5)

    assert asyncio.run(main()) == 0

    assert venue.bus_connected_at_start, "the bus must be up before the venue is connected"
    assert "venue.read" in timeline, "the barrier must actually read for the proof to bite"
    assert timeline.index("exchange.start") < timeline.index("venue.read")


class _TimelineFeed(_BlockingFeed):
    """A ``_BlockingFeed`` that records the runner cutting it, so teardown
    order is observable at the seam."""

    def __init__(self, timeline: list[str]) -> None:
        super().__init__()
        self._timeline = timeline

    async def stop(self) -> None:
        self._timeline.append("feed.stop")


class _TimelineStore(SQLiteStore):
    """The real store, recording the one teardown step it owns — the last."""

    def __init__(self, path: Path, timeline: list[str]) -> None:
        super().__init__(path)
        self._timeline = timeline

    def close(self) -> None:
        self._timeline.append("store.close")
        super().close()


class _VenueWatchingTheCadences(_LifecycleRecordingVenue):
    """Records, at the moment the runner releases it, whether the reconcile
    cadences are still running. The timeline alone cannot show this: a cancelled
    cadence appends nothing of its own, and the tasks are the runner's — there
    is no seam to observe them through, so the test reads the attribute."""

    def __init__(self, timeline: list[str]) -> None:
        super().__init__(timeline)
        self.engine: Engine | None = None
        self.cadences_still_running = True

    async def stop(self) -> None:
        assert self.engine is not None, "the test must hand the venue its engine"
        self.cadences_still_running = any(not task.done() for task in self.engine._cadence_tasks)
        await super().stop()


def test_the_reverse_shutdown_releases_the_exchange_once_the_cadences_are_cancelled(
    tmp_path: Path,
) -> None:
    """ADR-0024's reverse shutdown: ``exchange.stop`` sits behind ``feed.stop``
    and behind ``reconcile.stop``, and ahead of the drain — the venue is
    released only once nothing is left to read it, and while the bus it may
    still report on is open. The cadences call ``fetch_order``: releasing the
    adapter under a live cycle would freeze that cycle against an adapter the
    runner itself had just torn down (ADR-0011 inv 1)."""
    timeline: list[str] = []
    venue = _VenueWatchingTheCadences(timeline)

    async def main() -> int:
        feed = _TimelineFeed(timeline)
        engine = Engine(
            bus=InMemoryBus(),
            clock=ManualClock(),
            store=_TimelineStore(tmp_path / "saga.db", timeline),
            exchange=venue,
            feed=feed,
        )
        venue.engine = engine
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(feed.started.wait(), timeout=5)
        await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5)

    assert asyncio.run(main()) == 0

    assert timeline == ["exchange.start", "feed.stop", "exchange.stop", "store.close"]
    assert not venue.cadences_still_running, (
        "the venue must not be released while a reconcile cycle can still read it"
    )


def test_the_fault_path_stops_the_exchange_in_the_same_position_as_a_graceful_stop(
    tmp_path: Path,
) -> None:
    """The faulted teardown differs from the graceful one in failure *policy*,
    never in membership or order (ADR-0024): a fault must release the venue
    too, and in the same place — a second copy of the sequence is what this
    one ordered membership exists to prevent."""
    timeline: list[str] = []

    async def faulted_life() -> tuple[int, Engine]:
        engine = Engine(
            bus=InMemoryBus(),
            clock=ManualClock(),
            store=_TimelineStore(tmp_path / "saga.db", timeline),
            exchange=_LifecycleRecordingVenue(timeline),
            feed=_FaultingFeed(timeline),
        )
        return await engine.run(), engine

    exit_code, engine = asyncio.run(faulted_life())

    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED
    assert timeline == ["exchange.start", "feed.stop", "exchange.stop", "store.close"]


class _CadenceProbingLiveVenue(_CadenceWatchingLiveVenue):
    """A live venue that probes, at the moment the runner releases it, whether
    an account cycle can still reach it.

    Under a ``ManualClock`` nothing moves virtual time during a teardown, so a
    case that merely asserted "no further read" would pass against a cadence
    still parked on its next deadline — alive, and simply never woken. Crossing
    a deadline *here* is what makes the assertion sensitive: a cadence the
    sequence had not already cut would answer it, reading an adapter this very
    sequence is in the middle of tearing down (ADR-0011 inv 1).
    """

    def __init__(self) -> None:
        super().__init__()
        self.clock: ManualClock | None = None
        self.reads_at_release: int | None = None

    async def stop(self) -> None:
        assert self.clock is not None, "the test must hand the venue its clock"
        self.reads_at_release = self.account_reads
        self.clock.advance_to(self.clock.timestamp_ns() + _ACCOUNT_INTERVAL_NS)
        for _ in range(5):  # every slot a surviving cycle would need to reach it
            await asyncio.sleep(0)
        await super().stop()


def _live_life_probing_its_teardown(
    tmp_path: Path, *, fault: bool
) -> tuple[int, ComponentState, _CadenceProbingLiveVenue]:
    """One live life ended by ``fault``'s path — a broken feed, or an asked-for
    stop — with the venue probing the release slot on the way out."""
    venue = _CadenceProbingLiveVenue()

    async def one_life() -> tuple[int, ComponentState]:
        bus = InMemoryBus()
        clock = ManualClock()
        venue.clock = clock
        feed: _FaultingFeed | _BlockingFeed = _FaultingFeed() if fault else _BlockingFeed()
        engine = Engine(
            bus=bus,
            clock=clock,
            store=SQLiteStore(tmp_path / "saga.db"),
            exchange=venue,
            feed=feed,
            config=EngineConfig(
                reconcile=ReconcileConfig(account_interval_seconds=_ACCOUNT_INTERVAL)
            ),
        )
        run = asyncio.create_task(engine.run())
        if isinstance(feed, _BlockingFeed):
            await asyncio.wait_for(feed.started.wait(), timeout=5)
            # One cycle under supervision first, so the case is about a cadence
            # that was genuinely running being cut — not one that never started.
            clock.advance_to(_ACCOUNT_INTERVAL_NS)
            await asyncio.wait_for(venue.cycled.wait(), timeout=5)
            await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5), engine.state

    exit_code, state = asyncio.run(one_life())
    return exit_code, state, venue


def test_a_graceful_stop_silences_the_account_cadence_before_releasing_the_venue(
    tmp_path: Path,
) -> None:
    """``reconcile.stop`` ahead of ``exchange.stop`` covers the account grain
    too (ADR-0024): the cycle reads the venue, so releasing the adapter under a
    live one would freeze that cycle against a teardown the runner itself had
    just performed — an outage indistinguishable from the venue's own."""
    exit_code, state, venue = _live_life_probing_its_teardown(tmp_path, fault=False)

    assert (exit_code, state) == (0, ComponentState.STOPPED)
    assert venue.reads_at_release == 2, "the barrier's read, then the one supervised cycle"
    assert venue.account_reads == venue.reads_at_release, (
        "the account cadence must be silent by the time the venue is released"
    )


def test_the_fault_path_silences_the_account_cadence_in_the_same_place(
    tmp_path: Path,
) -> None:
    """The faulted teardown differs in failure *policy*, never in membership
    (ADR-0024), and the account cadence is inside that membership on both
    paths. What it pins here that the graceful case cannot: the cycle is a task
    in the runner's own ``TaskGroup``, so an abort cancels it with its siblings
    — a cadence spawned outside that group would survive the fault and read a
    venue nobody is supervising any more."""
    exit_code, state, venue = _live_life_probing_its_teardown(tmp_path, fault=True)

    assert exit_code != 0
    assert state is ComponentState.FAULTED
    assert venue.reads_at_release == 1, "the barrier's read alone: the feed broke before a cycle"
    assert venue.account_reads == venue.reads_at_release, (
        "a faulted run leaves no cycle behind to read the venue it is releasing"
    )


class _RefusingVenue(_LifecycleRecordingVenue):
    """A venue that refuses to align at connect time — the shape ADR-0044's
    leverage mismatch and ADR-0046's unsupported account mode will take."""

    async def start(self) -> None:
        raise InvariantViolation("the venue refused to align with this config")


def test_a_venue_that_refuses_to_start_faults_the_engine_before_any_order(
    tmp_path: Path,
) -> None:
    """Why ``start()`` sits where it does (ADR-0024 step 4): a refusal is an
    ``InvariantViolation`` on the existing fail-fast policy, and it lands
    before the barrier — so the run that would otherwise have placed an order
    against an unaligned venue never reaches the venue at all."""
    timeline: list[str] = []
    venue = _RefusingVenue(timeline)

    async def refused_life() -> tuple[int, Engine]:
        bus = InMemoryBus()
        clock = ManualClock()
        engine = Engine(
            bus=bus,
            clock=clock,
            store=SQLiteStore(tmp_path / "saga.db"),
            exchange=venue,
            feed=ReplayFeed(path=_write_ticks(tmp_path / "ticks.jsonl"), bus=bus, clock=clock),
        )
        engine.register(
            SingleShotMarketStrategy(
                strategy_id="eager",
                bus=bus,
                clock=clock,
                portfolio=engine.portfolio_for("eager"),
                side=Side.BUY,
                quantity=Decimal("0.5"),
            ),
            symbols={"BTC"},
        )
        return await engine.run(), engine

    with capture_events() as logs:
        exit_code, engine = asyncio.run(refused_life())

    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED
    assert "engine.faulted" in [log["event"] for log in logs]
    # A registered strategy and a feed full of ticks, and still nothing placed
    # and nothing read: the refusal precedes both the barrier and the first
    # tick. The lone entry is the teardown releasing the venue anyway — the
    # same best-effort release the feed gets whether or not it ever started.
    assert timeline == ["exchange.stop"]


class _VenueThatBreaksOnStop(_LifecycleRecordingVenue):
    """A venue whose release breaks mid-teardown — the wedged-link shape."""

    async def stop(self) -> None:
        raise RuntimeError("the venue link broke during teardown")


def test_a_venue_that_breaks_on_stop_is_recorded_and_the_teardown_carries_on(
    tmp_path: Path,
) -> None:
    """ADR-0020/0024: the faulted teardown is best-effort per step but never
    silent. A venue that cannot be released is recorded as
    ``engine.stop_hook_failed`` under its own name — and it can neither mask
    the fault, block the non-zero exit, nor cost the store its close."""
    timeline: list[str] = []

    async def faulted_life() -> tuple[int, Engine]:
        engine = Engine(
            bus=InMemoryBus(),
            clock=ManualClock(),
            store=_TimelineStore(tmp_path / "saga.db", timeline),
            exchange=_VenueThatBreaksOnStop(timeline),
            feed=_FaultingFeed(timeline),
        )
        return await engine.run(), engine

    with capture_events() as logs:
        exit_code, engine = asyncio.run(faulted_life())

    names = [log["event"] for log in logs]
    assert exit_code != 0
    assert engine.state is ComponentState.FAULTED
    assert "engine.faulted" in names, "the broken release must not mask the fault"
    hook_failures = [log for log in logs if log["event"] == "engine.stop_hook_failed"]
    assert [log["hook"] for log in hook_failures] == ["exchange.stop"]
    # The steps behind the break still ran: the store is closed, not leaked.
    assert timeline == ["exchange.start", "feed.stop", "store.close"]


class _StoreThatBreaksOnClose(_TimelineStore):
    """The last teardown step, breaking — so the graceful pass fails *behind*
    the venue release, which is the only way to reach the second one."""

    def close(self) -> None:
        self._timeline.append("store.close")
        raise RuntimeError("the store would not close")


def test_a_graceful_teardown_that_breaks_releases_the_venue_a_second_time(
    tmp_path: Path,
) -> None:
    """Why ``Exchange.stop()`` must be idempotent: the two teardown paths are
    one membership, and the faulted path walks it **from the top**. A graceful
    step that raises behind the venue release (here the store's close) faults
    the run, and the best-effort pass releases the venue again — so an adapter
    is called twice in the one shutdown and may hold nothing the second time."""
    timeline: list[str] = []

    async def main() -> tuple[int, Engine]:
        feed = _TimelineFeed(timeline)
        engine = Engine(
            bus=InMemoryBus(),
            clock=ManualClock(),
            store=_StoreThatBreaksOnClose(tmp_path / "saga.db", timeline),
            exchange=_LifecycleRecordingVenue(timeline),
            feed=feed,
        )
        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(feed.started.wait(), timeout=5)
        await asyncio.wait_for(engine.stop(), timeout=5)
        return await asyncio.wait_for(run, timeout=5), engine

    exit_code, engine = asyncio.run(main())

    assert exit_code != 0, "a teardown that breaks faults the run, graceful request or not"
    assert engine.state is ComponentState.FAULTED
    assert timeline.count("exchange.stop") == 2, "the release is driven once per teardown pass"
    assert timeline == [
        "exchange.start",
        "feed.stop",
        "exchange.stop",
        "store.close",
        "feed.stop",
        "exchange.stop",
        "store.close",
    ]


def test_graceful_stop_leaves_resting_live_orders_for_the_next_start_to_re_adopt(
    tmp_path: Path,
) -> None:
    """ADR-0024: a graceful stop does not cancel resting ``LIVE`` orders —
    crash and graceful stop converge on the one snapshot-plus-reconcile path."""
    db = tmp_path / "saga.db"
    clock = ManualClock()
    cloid = derive_cloid("resting:BTC:1")

    async def first_life() -> PaperExchange:
        """Rest a BUY limit below the market, then stop gracefully."""
        bus = InMemoryBus()
        store = SQLiteStore(db)
        venue = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
        )
        feed = ReplayFeed(path=_write_ticks(tmp_path / "first.jsonl"), bus=bus, clock=clock)
        engine = Engine(bus=bus, clock=clock, store=store, exchange=venue, feed=feed)
        engine.register(
            SingleShotLimitStrategy(
                strategy_id="resting",
                bus=bus,
                clock=clock,
                side=Side.BUY,
                quantity=Decimal("0.5"),
                price=Decimal("41000"),
            ),
            symbols={"BTC"},
        )

        live = asyncio.Event()

        async def on_order_event(event: OrderEvent) -> None:
            if event.cloid == cloid and event.state is OrderState.LIVE:
                live.set()

        bus.subscribe(OrderEvent, on_order_event)

        run = asyncio.create_task(engine.run())
        await asyncio.wait_for(live.wait(), timeout=5)
        await engine.stop()
        assert await run == 0
        return venue

    venue = asyncio.run(first_life())

    # The resting LIVE order survived the graceful stop, checkpointed, untouched.
    between = SQLiteStore(db)
    try:
        rested = between.get_order(cloid)
        assert rested is not None
        assert rested.state is OrderState.LIVE
    finally:
        between.close()

    async def second_life(venue: PaperExchange) -> tuple[int, list[OrderEvent]]:
        """Restart over the surviving store and venue: the barrier re-adopts.

        The venue outlives the process (its push link from the first life is
        dead, exactly as after a crash); only ``fetch_order`` venue truth and
        the durable store connect the two lives.
        """
        bus = InMemoryBus()
        store = SQLiteStore(db)
        # Fresh non-crossing ticks, timestamped after the first life — virtual
        # time never moves backward across lives on the shared clock.
        later = [
            dict(r, ts_event=int(r["ts_event"]) + 10_000, trade_id=f"2-{r['trade_id']}")
            for r in _ROWS
        ]
        (tmp_path / "second.jsonl").write_text("\n".join(json.dumps(r) for r in later) + "\n")
        feed = ReplayFeed(path=tmp_path / "second.jsonl", bus=bus, clock=clock)
        engine = Engine(bus=bus, clock=clock, store=store, exchange=venue, feed=feed)
        engine.register(
            SingleShotLimitStrategy(
                strategy_id="resting",
                bus=bus,
                clock=clock,
                side=Side.BUY,
                quantity=Decimal("0.5"),
                price=Decimal("41000"),
            ),
            symbols={"BTC"},
        )

        events: list[OrderEvent] = []

        async def record(event: OrderEvent) -> None:
            events.append(event)

        bus.subscribe(OrderEvent, record)

        run = asyncio.create_task(engine.run())
        while engine.state is not ComponentState.RUNNING and not run.done():
            await asyncio.sleep(0)
        if run.done():
            return await run, events  # surfaces a startup failure instead of hanging
        await engine.stop()
        return await run, events

    exit_code, events = asyncio.run(second_life(venue))

    assert exit_code == 0
    # Re-adopted, not resolved away: the saga is still LIVE and nothing failed.
    assert not any(e.state is OrderState.FAILED for e in events)
    after = SQLiteStore(db)
    try:
        adopted = after.get_order(cloid)
        assert adopted is not None
        assert adopted.state is OrderState.LIVE
    finally:
        after.close()
    # The venue still holds exactly the one resting order — no duplicate send.
    view = asyncio.run(venue.fetch_order(cloid))
    assert isinstance(view, VenueOrderView) and view.has_record
