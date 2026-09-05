"""The catalog-walking contract: every named event has a path, every path a test.

"A state-affecting path with no named event is a defect" (ADR-0020) is only
enforceable if the catalog is walkable and each name is proven to be emitted by
a test that drives its real path. This module *is* that walk: one scenario per
``NamedEvent``, each exercising the shipped path end-to-end (no mocks of our own
classes), plus a coverage assertion that the walk names every catalog member —
so a future slice that adds a name without a path-and-test fails here.

The scenarios deliberately re-drive the paths in miniature rather than lean on
the layer suites, so the walk reads as a single, self-contained census of the
engine's observable surface.
"""

import asyncio
from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

import pytest
from hyperliquid_fakes import (
    TEST_SIGNING_KEY,
    FakeExchangeApi,
    FakeWsConnection,
    trade,
    trades_frame,
)
from ledgers import GENESIS, checkpointer
from pydantic import SecretStr
from venue_doubles import DERIVED_STATE, LiveVenueDouble, VenueDouble, account_state

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AccountModeVerdict,
    AggressorSide,
    Exchange,
    ExecutionReport,
    FillReport,
    InstrumentSpec,
    InvariantViolation,
    LeverageBook,
    LeverageSpec,
    MarketTick,
    Order,
    OrderEvent,
    OrderState,
    OrderStatusReport,
    PlaceOrder,
    PlaceSignal,
    PreTradeGuard,
    Side,
    Signal,
    VenueOrderView,
    VenueReadFailure,
    derive_cloid,
)
from tickwright.domain.enums import OrderType, TimeInForce
from tickwright.engine.cache import Cache
from tickwright.engine.checkpoint import Checkpointer
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.guard import RealGuard
from tickwright.engine.ledger_reconcile import LedgerReconciliation
from tickwright.engine.reconcile import ReconcileConfig, Reconciler
from tickwright.engine.runner import Engine
from tickwright.engine.strategy_host import StrategyHost
from tickwright.observability import NamedEvent
from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid import (
    HyperliquidConfig,
    HyperliquidExchange,
    HyperliquidFeed,
    HyperliquidUniverse,
)

_SPEC = InstrumentSpec(
    symbol="BTC", sz_decimals=3, max_decimals=6, max_sig_figs=5, min_notional=Decimal("10")
)


# --- Builders ----------------------------------------------------------------


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


def _market_signal(*, quantity: str = "0.5", seq: int = 1, side: Side = Side.BUY) -> PlaceSignal:
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


def _limit_signal(*, price: str, quantity: str = "0.5") -> PlaceSignal:
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=1,
        side=Side.BUY,
        quantity=Decimal(quantity),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        price=Decimal(price),
    )


def _saga(state: OrderState, *, cloid: str = "0xabc") -> Order:
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


def _status(status: OrderState, *, cloid: str = "0xabc") -> OrderStatusReport:
    return OrderStatusReport(
        ts_event=2_000, ts_init=2_000, cloid=cloid, symbol="BTC", status=status
    )


class _SilentExchange(VenueDouble):
    """A venue that accepts a placement and says nothing back — an order rests
    ``SUBMITTED`` so a synthetic status/fill can drive the next transition."""

    async def place(self, order: PlaceOrder) -> None:
        return None

    async def cancel(self, cloid: str) -> None:
        return None

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        return VenueReadFailure.SEND_FAILED


class _ForgetfulVenue(VenueDouble):
    """A venue that answers positively but has lost this order (``status=None``)."""

    async def place(self, order: PlaceOrder) -> None:
        raise AssertionError("the reconcile walk never places")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("the reconcile walk never cancels")

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        return VenueOrderView(status=None)


class _LiveShapedVenue(LiveVenueDouble):
    """A venue whose genesis is *ingested* rather than declared — so the startup
    barrier has an account row to create, and reads this venue to do it."""

    async def place(self, order: PlaceOrder) -> None:
        raise AssertionError("the materialisation walk never places")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("the materialisation walk never cancels")

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        return VenueOrderView(status=None)


class _DarkVenue(VenueDouble):
    """A venue whose reads all fail (``SEND_FAILED``) — the connectivity guard trips."""

    async def place(self, order: PlaceOrder) -> None:
        raise AssertionError("the frozen cycle never places")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("the frozen cycle never cancels")

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        return VenueReadFailure.SEND_FAILED


class _Strategy:
    """A minimal strategy whose handlers raise, to exercise containment."""

    def __init__(self, strategy_id: str = "trivial") -> None:
        self.strategy_id = strategy_id

    async def on_tick(self, tick: MarketTick) -> None:
        raise KeyError("third-party bug")

    async def on_order_event(self, event: OrderEvent) -> None:
        return None

    def set_next_seq(self, next_seq: int) -> None:
        return None

    def snapshot(self) -> bytes:
        return b""

    def restore(self, data: bytes) -> None:
        raise ValueError("unknown snapshot version")


# --- Wiring ------------------------------------------------------------------


def _manager(
    bus: InMemoryBus,
    clock: ManualClock,
    exchange: Exchange,
    *,
    guard: PreTradeGuard | None = None,
    checks: Checkpointer | None = None,
) -> None:
    """Wire an ``ExecutionManager`` over ``exchange`` onto ``bus`` — the exchange
    must already share ``bus`` so its fills flow back to the manager."""
    manager = ExecutionManager(
        bus=bus,
        exchange=exchange,
        checkpointer=checks
        if checks is not None
        else checkpointer(SQLiteStore(":memory:"), clock=clock),
        guard=guard,
    )
    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)


def _reconciler(state: OrderState, venue: Exchange, config: ReconcileConfig) -> Reconciler:
    clock = ManualClock(start_ns=0)
    store = SQLiteStore(":memory:")
    store.checkpoint(_saga(state), ts_ns=500)
    cache = Cache(store=store)
    cache.rebuild()
    return Reconciler(
        bus=InMemoryBus(),
        clock=clock,
        exchange=venue,
        cache=cache,
        config=config,
    )


# --- Scenarios: each drives one path that must emit its named event ----------


def _drive_market_fill() -> None:
    """MARKET order through the paper exchange: placed → submitted → filled."""
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
    )
    _manager(bus, clock, exchange)

    async def go() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_market_signal())

    asyncio.run(go())


def _drive_partial_fill() -> None:
    bus = InMemoryBus()
    _manager(bus, ManualClock(start_ns=1_000), _SilentExchange())
    cloid = derive_cloid("trivial:BTC:1")

    async def go() -> None:
        await bus.publish(_market_signal(quantity="1.0"))  # rests SUBMITTED
        await bus.publish(
            FillReport(
                ts_event=2_000,
                ts_init=2_000,
                cloid=cloid,
                symbol="BTC",
                trade_id="f1",
                quantity=Decimal("0.4"),
                price=Decimal("42000"),
            )
        )

    asyncio.run(go())


def _drive_position_changes(*, closing: bool) -> Callable[[], None]:
    """A MARKET buy through the paper venue moves the ledger on the fill-apply
    path — ``position.opened``. A second, opposite fill on the same partition
    closes it, and the two names are the only place a position change is
    observable at all: it is an output derived from a fill already on the bus,
    never a bus event of its own (ADR-0045 §1)."""

    def scenario() -> None:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=1_000)
        exchange = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=ImmediateFillModel(),
            genesis_collateral=GENESIS,
            account_net=dict,
        )
        _manager(bus, clock, exchange)

        async def go() -> None:
            await bus.publish(_tick("42000"))
            await bus.publish(_market_signal())
            if closing:
                await bus.publish(_market_signal(seq=2, side=Side.SELL))

        asyncio.run(go())

    return scenario


def _drive_position_changed() -> None:
    """Adding to an open position — the ``changed`` arm, which needs a *second*
    fill on the same side rather than a second order going the other way."""
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=dict,
    )
    _manager(bus, clock, exchange)

    async def go() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_market_signal())
        await bus.publish(_market_signal(seq=2))

    asyncio.run(go())


def _drive_account_materialised() -> None:
    """A live first start: the startup barrier reads the venue account and
    creates the ledger's row from it (ADR-0042 §6, ADR-0043 §6). The venue
    declares no genesis — the live shape — so the row does not exist until the
    barrier makes it."""

    async def go() -> None:
        bus = InMemoryBus()
        clock = ManualClock()
        engine = Engine(
            bus=bus,
            clock=clock,
            store=SQLiteStore(":memory:"),
            exchange=_LiveShapedVenue(),
            feed=_IdleFeed(),
        )
        run = asyncio.create_task(engine.run())
        await engine.stop()
        assert await run == 0

    asyncio.run(go())


def _drive_denied() -> None:
    store = SQLiteStore(":memory:")
    guard = RealGuard(specs={"BTC": _SPEC}, store=store, clock=ManualClock(start_ns=1_000))
    bus = InMemoryBus()
    _manager(bus, ManualClock(start_ns=1_000), _SilentExchange(), guard=guard)

    async def go() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal(price="100", quantity="0.05"))  # below min notional

    asyncio.run(go())


def _drive_status(*statuses: OrderState) -> Callable[[], None]:
    def scenario() -> None:
        bus = InMemoryBus()
        _manager(bus, ManualClock(start_ns=1_000), _SilentExchange())
        cloid = derive_cloid("trivial:BTC:1")

        async def go() -> None:
            await bus.publish(_market_signal())  # rests SUBMITTED
            for status in statuses:
                await bus.publish(_status(status, cloid=cloid))

        asyncio.run(go())

    return scenario


def _drive_kill_switch(*, reset: bool) -> Callable[[], None]:
    def scenario() -> None:
        guard = RealGuard(specs={"BTC": _SPEC}, store=SQLiteStore(":memory:"), clock=ManualClock())
        guard.trip_kill_switch("operator halt")
        if reset:
            guard.reset_kill_switch()

    return scenario


def _drive_strategy_error() -> None:
    bus = InMemoryBus()
    host = StrategyHost(bus=bus, clock=ManualClock(), store=SQLiteStore(":memory:"))
    host.register(_Strategy(), symbols={"BTC"})
    host.start()

    async def go() -> None:
        await bus.publish(_tick("42000"))

    asyncio.run(go())


def _drive_snapshot_incompatible() -> None:
    store = SQLiteStore(":memory:")
    store.save_strategy_snapshot("trivial", b"old-shape", ts_ns=1_000)
    host = StrategyHost(bus=InMemoryBus(), clock=ManualClock(), store=store)
    host.register(_Strategy(), symbols={"BTC"})
    host.start()  # restore() raises → strategy.snapshot_incompatible


def _drive_inflight_reconciled() -> None:
    config = ReconcileConfig(inflight_interval_seconds=5.0, inflight_max_attempts=1)
    reconciler = _reconciler(OrderState.SUBMITTED, _ForgetfulVenue(), config)
    assert asyncio.run(reconciler.reconcile_inflight()) is True


def _drive_recency_skipped() -> None:
    clock = ManualClock(start_ns=1_000_000_000)
    store = SQLiteStore(":memory:")
    cache = Cache(store=store)
    # An in-session checkpoint makes the saga's recency known and fresh, so an
    # absent venue read is skipped inside the protection window (ADR-0011 inv 3).
    cache.checkpoint(_saga(OrderState.LIVE), ts_ns=clock.timestamp_ns())
    reconciler = Reconciler(
        bus=InMemoryBus(),
        clock=clock,
        exchange=_ForgetfulVenue(),
        cache=cache,
        config=ReconcileConfig(recent_order_protection_seconds=30.0, ghost_grace_seconds=90.0),
    )
    assert asyncio.run(reconciler.reconcile_open_orders()) is True


def _drive_ghost_reconciled() -> None:
    clock = ManualClock(start_ns=0)
    store = SQLiteStore(":memory:")
    store.checkpoint(_saga(OrderState.LIVE), ts_ns=500)
    cache = Cache(store=store)
    cache.rebuild()  # store-recovered: recency unknown, only the grace window guards
    reconciler = Reconciler(
        bus=InMemoryBus(),
        clock=clock,
        exchange=_ForgetfulVenue(),
        cache=cache,
        config=ReconcileConfig(ghost_grace_seconds=90.0),
    )

    async def go() -> None:
        assert await reconciler.reconcile_open_orders() is True  # arms the grace clock
        await clock.sleep(91.0)
        assert await reconciler.reconcile_open_orders() is True  # continuous absence → ghost

    asyncio.run(go())


def _drive_frozen() -> None:
    reconciler = _reconciler(OrderState.SUBMITTED, _DarkVenue(), ReconcileConfig())
    assert asyncio.run(reconciler.reconcile_inflight()) is False


def _drive_exchange_request_failed() -> None:
    """A live placement whose transport fails: the adapter names the failure
    and emits no report — the send window's truth is unknown, and
    reconcile-by-cloid owns it (``HyperliquidExchange``, ADR-0008 rule 2)."""

    async def go() -> None:
        exchange = HyperliquidExchange(
            config=HyperliquidConfig(
                testnet=True,
                symbols=["BTC"],
                signing_key=SecretStr(TEST_SIGNING_KEY),
            ),
            bus=InMemoryBus(),
            clock=ManualClock(),
            universe=HyperliquidUniverse(specs={"BTC": _SPEC}, asset_indices={"BTC": 0}),
            startup_timeout_seconds=60.0,
            post=FakeExchangeApi({"order": ConnectionError("connection refused")}),
        )
        await exchange.place(
            PlaceOrder(
                cloid=derive_cloid("walk:BTC:1"),
                symbol="BTC",
                side=Side.BUY,
                quantity=Decimal("0.5"),
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                price=Decimal("100"),
            )
        )

    asyncio.run(go())


def _drive_exchange_action_rejected() -> None:
    """A live placement the venue refuses at the action envelope (bad
    nonce/signature/rate-limit): the adapter names the refusal and emits no
    terminal — reconcile-by-cloid owns the in-flight order (``HyperliquidExchange``,
    ADR-0008 rule 2)."""

    async def go() -> None:
        exchange = HyperliquidExchange(
            config=HyperliquidConfig(
                testnet=True,
                symbols=["BTC"],
                signing_key=SecretStr(TEST_SIGNING_KEY),
            ),
            bus=InMemoryBus(),
            clock=ManualClock(),
            universe=HyperliquidUniverse(specs={"BTC": _SPEC}, asset_indices={"BTC": 0}),
            startup_timeout_seconds=60.0,
            post=FakeExchangeApi({"order": {"status": "err", "response": "Invalid nonce"}}),
        )
        await exchange.place(
            PlaceOrder(
                cloid=derive_cloid("walk:BTC:1"),
                symbol="BTC",
                side=Side.BUY,
                quantity=Decimal("0.5"),
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                price=Decimal("100"),
            )
        )

    asyncio.run(go())


def _drive_exchange_leverage_unchanged() -> None:
    """A live boot whose venue already agrees: the leverage push declines to
    write and names the symbol it left alone (``preflight``, ADR-0044 §7).

    The skip branch is the only path that emits this — a no-op ``updateLeverage``
    answers with the same ``ok`` envelope a real change does — so the account
    read has to come back holding a position at exactly the configured pair.
    ``1x``/isolated is that pair here because ``_SPEC`` publishes no larger cap,
    and §9's bound runs ahead of the push."""

    async def go() -> None:
        exchange = HyperliquidExchange(
            config=HyperliquidConfig(
                testnet=True,
                symbols=["BTC"],
                signing_key=SecretStr(TEST_SIGNING_KEY),
            ),
            bus=InMemoryBus(),
            clock=ManualClock(),
            universe=HyperliquidUniverse(specs={"BTC": _SPEC}, asset_indices={"BTC": 0}),
            startup_timeout_seconds=60.0,
            post=FakeExchangeApi(
                {
                    "userAbstraction": "disabled",
                    "clearinghouseState": {
                        "assetPositions": [
                            {
                                "position": {
                                    "coin": "BTC",
                                    "leverage": {"type": "isolated", "value": 1},
                                }
                            }
                        ]
                    },
                }
            ),
            leverage=LeverageBook(entries={"BTC": LeverageSpec(mode="isolated", leverage=1)}),
        )
        await exchange.start()

    asyncio.run(go())


class _IdleFeed:
    """A feed with nothing to say — the lifecycle walk needs the ordered
    startup, not ticks. A ``MarketFeed`` double at the venue boundary."""

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


class _PoisonedFeed:
    """A feed whose read loop breaks an engine assumption — the fail-fast class."""

    async def start(self) -> None:
        raise InvariantViolation("the read loop broke an engine assumption")

    async def stop(self) -> None:
        return None


def _drive_engine_faulted() -> None:
    """An ``InvariantViolation`` in a supervised task: cancel, fault, exit 1."""

    async def go() -> None:
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
            feed=_PoisonedFeed(),
        )
        assert await engine.run() == 1

    asyncio.run(go())


class _StoreThatBreaksOnClose(SQLiteStore):
    """An in-memory store whose ``close`` fails — the fault-teardown hook that
    breaks. A ``Store`` at the boundary; every other method is the real one."""

    def close(self) -> None:
        raise RuntimeError("store close broke during fault teardown")


def _drive_engine_stop_hook_failed() -> None:
    """A fault whose best-effort teardown hits a hook that raises: the break is
    recorded (never swallowed silently), and the fault still exits non-zero."""

    async def go() -> None:
        bus = InMemoryBus()
        clock = ManualClock()
        engine = Engine(
            bus=bus,
            clock=clock,
            store=_StoreThatBreaksOnClose(":memory:"),
            exchange=PaperExchange(
                bus=bus,
                clock=clock,
                fill_model=ImmediateFillModel(),
                genesis_collateral=GENESIS,
                account_net=dict,
            ),
            feed=_PoisonedFeed(),
        )
        assert await engine.run() == 1

    asyncio.run(go())


def _drive_engine_lifecycle() -> None:
    """One supervised startup-and-stop: barrier cleared, feed started (ADR-0024)."""

    async def go() -> None:
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
            feed=_IdleFeed(),
        )
        run = asyncio.create_task(engine.run())
        await engine.stop()
        assert await run == 0

    asyncio.run(go())


def _drive_feed_lagged() -> None:
    """A stalled consumer while more BTC trades arrive: the live feed conflates
    at ingress — keep-latest-per-symbol — and names the drop (ADR-0023)."""

    async def go() -> None:
        bus = InMemoryBus()
        clock = ManualClock()
        release = asyncio.Event()
        stalled = asyncio.Event()

        async def slow_consumer(tick: MarketTick) -> None:
            stalled.set()
            await release.wait()

        bus.subscribe(MarketTick, slow_consumer)
        connection = FakeWsConnection(
            [
                trades_frame(trade("BTC", "42000", 1)),
                trades_frame(trade("BTC", "42001", 2)),
                trades_frame(trade("BTC", "42002", 3)),
            ]
        )

        async def connect(url: str) -> FakeWsConnection:
            return connection

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]), bus=bus, clock=clock, connect=connect
        )
        run = asyncio.create_task(feed.start())
        await stalled.wait()
        # While the first publish is stuck, 42001 lands unpublished and 42002
        # supersedes it — the drop that emits feed.lagged.
        await connection.drained.wait()
        release.set()
        await feed.stop()
        await run

    asyncio.run(go())


def _drive_feed_frame_dropped() -> None:
    """A malformed trades frame reaches the live feed: it is skipped and named
    (``feed.frame_dropped``, ADR-0023), and the good frame after it still ticks
    through — the engine is never faulted by one bad row."""

    async def go() -> None:
        bus = InMemoryBus()
        clock = ManualClock()
        ticked = asyncio.Event()

        async def record(tick: MarketTick) -> None:
            ticked.set()

        bus.subscribe(MarketTick, record)
        connection = FakeWsConnection(
            [
                trades_frame({"coin": "BTC", "side": "B", "px": "not-a-number"}),
                trades_frame(trade("BTC", "100", 1)),
            ]
        )

        async def connect(url: str) -> FakeWsConnection:
            return connection

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]), bus=bus, clock=clock, connect=connect
        )
        run = asyncio.create_task(feed.start())
        await ticked.wait()  # the good frame landed → the feed survived the bad one
        await feed.stop()
        await run

    asyncio.run(go())


def _drive_account_reconciled() -> None:
    """One account-grain cycle against a venue snapshot: the ledger is
    cross-checked, the pass is named (``account.reconciled``, ADR-0034), and the
    Tier-1 gap it finds is healed and named too (``account.healed``).

    Both names fall out of the one cycle because the snapshot carries a position
    the ledger has never seen — materialisation opens a cash line and no book —
    so this pass is a real divergence rather than a staged one, and the heal it
    books is the record's own path.

    Live-shaped by construction — a ledger whose genesis was ingested from the
    venue — because paper has no second account for a cycle to compare against
    and so never schedules one."""

    async def go() -> None:
        venue = _LiveShapedVenue()
        keeper = Checkpointer(
            spec=venue.account_spec(), store=SQLiteStore(":memory:"), clock=ManualClock()
        )
        keeper.recover()
        keeper.portfolio.materialise(DERIVED_STATE)
        await LedgerReconciliation(exchange=venue, checkpointer=keeper).reconcile_account()

    asyncio.run(go())


def _drive_account_mode_unverified() -> None:
    """The same cycle, against a venue whose account abstraction mode no longer
    verifies: the Tier-1 cash heal is refused and the refusal is named
    (``account.mode_unverified``, ADR-0046 §4).

    The pass still finds its divergence and still records itself — this is a
    freeze of the account-grain cross-check, not a fault — so the name arrives
    off the same healthy cycle rather than a broken one.

    The venue's equity is moved off the one the ledger was materialised from,
    because the guard runs *only* where there is a cash heal to make: a snapshot
    agreeing on cash reaches no mode read at all, which is the affordability
    ADR-0046 §4 is built on and would leave this scenario silently driving
    nothing."""

    class _SwitchedVenue(_LiveShapedVenue):
        async def verify_account_mode(self) -> AccountModeVerdict:
            return AccountModeVerdict.CHANGED

    async def go() -> None:
        venue = _SwitchedVenue(state=account_state("30.0", "-0.034"))
        keeper = Checkpointer(
            spec=venue.account_spec(), store=SQLiteStore(":memory:"), clock=ManualClock()
        )
        keeper.recover()
        keeper.portfolio.materialise(DERIVED_STATE)
        await LedgerReconciliation(exchange=venue, checkpointer=keeper).reconcile_account()

    asyncio.run(go())


def _drive_account_reconcile_frozen() -> None:
    """The account cycle's anchor read fails: the cycle freezes, heals nothing
    and says so (``account.reconcile_frozen``, ADR-0011 inv 1) — the record an
    operator needs to tell a cross-check that stopped from a book that agreed."""

    async def go() -> None:
        venue = _LiveShapedVenue(state=None)
        keeper = Checkpointer(
            spec=venue.account_spec(), store=SQLiteStore(":memory:"), clock=ManualClock()
        )
        keeper.recover()
        await LedgerReconciliation(exchange=venue, checkpointer=keeper).reconcile_account()

    asyncio.run(go())


def _drive_valuation_divergence() -> None:
    """The same account cycle, against a snapshot whose *valuation* disagrees:
    the recomputed equity is outside the band and the gap is alerted rather than
    healed (``valuation.divergence``, ADR-0040 §6).

    The venue's equity and its unrealized leg are moved **together**, so that
    ``venue_cash`` lands back on the line the ledger was materialised at. That
    is what keeps the scenario driving the path it names: a cash gap is Tier-1,
    and a Tier-1 finding at the account grain suppresses exactly this alert."""

    async def go() -> None:
        venue = _LiveShapedVenue(state=account_state("26.9264", "0.966"))
        keeper = Checkpointer(
            spec=venue.account_spec(), store=SQLiteStore(":memory:"), clock=ManualClock()
        )
        keeper.recover()
        keeper.portfolio.materialise(DERIVED_STATE)
        await LedgerReconciliation(exchange=venue, checkpointer=keeper).reconcile_account()

    asyncio.run(go())


def _drive_leverage_divergence() -> None:
    """The same account cycle, against a venue holding the position at a pair
    config never asked for: the drift is alerted and nothing is written back
    (``leverage.divergence``, ADR-0044 §10).

    The snapshot is the healthy one with its **setting** moved and every figure
    left alone, which is the whole point of the check — a leverage change never
    re-margins an open position, so a body that also moved ``marginUsed`` would
    be driving this name off a divergence some other tier would have caught."""

    async def go() -> None:
        drifted = replace(
            DERIVED_STATE,
            positions=tuple(
                replace(position, leverage=LeverageSpec(mode="cross", leverage=5))
                for position in DERIVED_STATE.positions
            ),
        )
        venue = _LiveShapedVenue(state=drifted)
        keeper = Checkpointer(
            spec=venue.account_spec(), store=SQLiteStore(":memory:"), clock=ManualClock()
        )
        keeper.recover()
        keeper.portfolio.materialise(DERIVED_STATE)
        await LedgerReconciliation(exchange=venue, checkpointer=keeper).reconcile_account()

    asyncio.run(go())


# --- The catalog walk --------------------------------------------------------

# Every NamedEvent → a scenario that drives its real path. Several of the saga
# events fall out of one lifecycle scenario; a status scenario is parametrized by
# the transition it feeds. A name missing from this map fails the coverage test.
SCENARIOS: dict[NamedEvent, Callable[[], None]] = {
    NamedEvent.ORDER_PLACED: _drive_market_fill,
    NamedEvent.ORDER_SUBMITTED: _drive_market_fill,
    NamedEvent.ORDER_FILLED: _drive_market_fill,
    NamedEvent.ORDER_PARTIALLY_FILLED: _drive_partial_fill,
    NamedEvent.ORDER_DENIED: _drive_denied,
    NamedEvent.ORDER_LIVE: _drive_status(OrderState.LIVE),
    NamedEvent.ORDER_REJECTED: _drive_status(OrderState.REJECTED),
    NamedEvent.ORDER_FAILED: _drive_status(OrderState.FAILED),
    NamedEvent.ORDER_CANCELLED: _drive_status(OrderState.LIVE, OrderState.CANCELLED),
    NamedEvent.POSITION_OPENED: _drive_position_changes(closing=False),
    NamedEvent.POSITION_CHANGED: _drive_position_changed,
    NamedEvent.POSITION_CLOSED: _drive_position_changes(closing=True),
    NamedEvent.ACCOUNT_MATERIALISED: _drive_account_materialised,
    NamedEvent.FEED_LAGGED: _drive_feed_lagged,
    NamedEvent.FEED_FRAME_DROPPED: _drive_feed_frame_dropped,
    NamedEvent.ENGINE_BARRIER_CLEARED: _drive_engine_lifecycle,
    NamedEvent.ENGINE_FEED_STARTED: _drive_engine_lifecycle,
    NamedEvent.ENGINE_FAULTED: _drive_engine_faulted,
    NamedEvent.ENGINE_STOP_HOOK_FAILED: _drive_engine_stop_hook_failed,
    NamedEvent.GUARD_KILL_SWITCH_TRIPPED: _drive_kill_switch(reset=False),
    NamedEvent.GUARD_KILL_SWITCH_RESET: _drive_kill_switch(reset=True),
    NamedEvent.STRATEGY_ERROR: _drive_strategy_error,
    NamedEvent.STRATEGY_SNAPSHOT_INCOMPATIBLE: _drive_snapshot_incompatible,
    NamedEvent.INFLIGHT_RECONCILED: _drive_inflight_reconciled,
    NamedEvent.RECONCILE_RECENCY_SKIPPED: _drive_recency_skipped,
    NamedEvent.GHOST_RECONCILED: _drive_ghost_reconciled,
    NamedEvent.RECONCILE_FROZEN: _drive_frozen,
    NamedEvent.ACCOUNT_RECONCILED: _drive_account_reconciled,
    NamedEvent.ACCOUNT_HEALED: _drive_account_reconciled,
    NamedEvent.ACCOUNT_MODE_UNVERIFIED: _drive_account_mode_unverified,
    NamedEvent.VALUATION_DIVERGENCE: _drive_valuation_divergence,
    NamedEvent.LEVERAGE_DIVERGENCE: _drive_leverage_divergence,
    NamedEvent.ACCOUNT_RECONCILE_FROZEN: _drive_account_reconcile_frozen,
    NamedEvent.EXCHANGE_REQUEST_FAILED: _drive_exchange_request_failed,
    NamedEvent.EXCHANGE_ACTION_REJECTED: _drive_exchange_action_rejected,
    NamedEvent.EXCHANGE_LEVERAGE_UNCHANGED: _drive_exchange_leverage_unchanged,
}


@pytest.mark.parametrize("event", list(NamedEvent), ids=lambda e: e.value)
def test_every_cataloged_event_is_emitted_by_its_path(event: NamedEvent) -> None:
    with capture_events() as logs:
        SCENARIOS[event]()

    emitted = {log["event"] for log in logs}
    assert event.value in emitted, f"{event.value} was not emitted by its scenario"


def test_the_walk_names_every_catalog_member() -> None:
    # The contract's teeth: a new NamedEvent with no scenario (hence no path and
    # no test) fails here — "a state-affecting path with no named event is a
    # defect", made executable (ADR-0020).
    assert set(SCENARIOS) == set(NamedEvent)
