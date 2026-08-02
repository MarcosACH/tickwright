"""``ExecutionManager`` — the one engine-internal saga orchestrator (ADR-0015).

It subscribes to ``Signal``s and raw ``ExecutionReport``s: on a ``PlaceSignal`` it
derives the ``cloid`` from the ``signal_id``, records the ``PENDING`` intent, and
publishes ``OrderPlaced`` → ``OrderSubmitted`` while sending to the exchange; on
the resulting ``FillReport`` it applies the saga transition and publishes the
canonical ``OrderFilled``. The exchange is real (``PaperExchange``) — we never mock
our own classes.
"""

import asyncio
import random
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from ledgers import GENESIS, checkpointer

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import (
    ImmediateFillModel,
    PaperExchange,
    StochasticFillModel,
    StochasticParams,
)
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Account,
    AggressorSide,
    CancelSignal,
    ExecutionReport,
    FillReport,
    InvariantViolation,
    MarketTick,
    Order,
    OrderCancelled,
    OrderEvent,
    OrderFilled,
    OrderLive,
    OrderPartiallyFilled,
    OrderPlaced,
    OrderRejected,
    OrderState,
    OrderStatusReport,
    OrderSubmitted,
    PlaceSignal,
    Position,
    Side,
    Signal,
    TimeInForce,
    derive_cloid,
)
from tickwright.domain.enums import OrderType
from tickwright.engine.cache import Cache
from tickwright.engine.checkpoint import Checkpointer
from tickwright.engine.execution import ExecutionManager
from tickwright.engine.portfolio import PortfolioProjection


def _market_signal(seq: int = 1) -> PlaceSignal:
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=seq,
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
    )


def _limit_signal(
    price: str,
    *,
    seq: int = 1,
    side: Side = Side.BUY,
    time_in_force: TimeInForce = TimeInForce.GTC,
    post_only: bool = False,
) -> PlaceSignal:
    return PlaceSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=seq,
        side=side,
        quantity=Decimal("0.5"),
        order_type=OrderType.LIMIT,
        time_in_force=time_in_force,
        price=Decimal(price),
        post_only=post_only,
    )


def _cancel_signal(target_signal_id: str, *, seq: int = 2) -> CancelSignal:
    return CancelSignal(
        ts_event=1_000,
        ts_init=1_000,
        strategy_id="trivial",
        symbol="BTC",
        seq=seq,
        target_signal_id=target_signal_id,
    )


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


@dataclass(frozen=True, slots=True)
class _Wiring:
    """The manager's collaborators, for the cases that read the two read-models
    rather than the durable record. ``_harness`` hands back the four every other
    case needs; this is the same wiring with the projections still in reach."""

    bus: InMemoryBus
    clock: ManualClock
    store: SQLiteStore
    checkpointer: Checkpointer
    order_events: list[OrderEvent]

    @property
    def cache(self) -> Cache:
        return self.checkpointer.cache

    @property
    def portfolio(self) -> PortfolioProjection:
        return self.checkpointer.portfolio


def _wiring(store: SQLiteStore) -> _Wiring:
    """The manager over its real collaborators, on the ``store`` handed in — so a
    case can substitute one that fails at a chosen seam.

    Both read-models come from the one ``Checkpointer`` built on that store, so
    this harness cannot express the split write the atomic path exists to close
    (ADR-0043 §4) even by accident."""
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    exchange = PaperExchange(
        bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
    )
    checks = checkpointer(store, clock=clock)
    manager = ExecutionManager(bus=bus, exchange=exchange, checkpointer=checks)

    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)

    order_events: list[OrderEvent] = []
    bus.subscribe(OrderEvent, lambda ev: _record(order_events, ev))
    return _Wiring(
        bus=bus,
        clock=clock,
        store=store,
        checkpointer=checks,
        order_events=order_events,
    )


def _harness(
    path: str | Path = ":memory:",
) -> tuple[InMemoryBus, ManualClock, SQLiteStore, list[OrderEvent]]:
    """The manager over its real collaborators. ``path`` backs the store with a
    file for the cases that must reopen it — closing a ``:memory:`` store takes
    the durable record with it, so a "what survived?" assertion needs a file."""
    wiring = _wiring(SQLiteStore(path))
    return wiring.bus, wiring.clock, wiring.store, wiring.order_events


def test_pending_intent_is_durable_before_the_send_can_crash() -> None:
    bus, _, store, _ = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        # No tick published: the real PaperExchange raises on place — standing
        # in for a crash mid-send. The write-ahead intent (ADR-0008 rule 1)
        # must already be durable, with the full params recovery needs to
        # reconcile by cloid.
        with pytest.raises(ValueError):
            await bus.publish(_market_signal())

    asyncio.run(scenario())

    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.PENDING
    assert record.side is Side.BUY
    assert record.quantity == Decimal("0.5")
    # PENDING is the only checkpoint so far: nothing was written after the send.
    assert store.history(cloid) == [(OrderState.PENDING, 1_000)]


def test_every_transition_is_checkpointed_on_the_happy_path() -> None:
    bus, _, store, _ = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick())
        await bus.publish(_market_signal())

    asyncio.run(scenario())

    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.FILLED
    assert record.cum_qty == Decimal("0.5")
    assert [state for state, _ in store.history(cloid)] == [
        OrderState.PENDING,
        OrderState.SUBMITTED,
        OrderState.FILLED,
    ]


def test_a_fill_makes_the_order_row_and_the_ledger_durable_together() -> None:
    """The money line's crash-safety rests on this one write (ADR-0043 §4).

    ``PaperExchange`` holds no position state and has no venue to heal from, so
    the store is the paper ledger's sole authority: a fill that reached the
    order row without reaching the positions row is a fill lost for good.
    """
    bus, _, store, _ = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick())
        await bus.publish(_market_signal())

    asyncio.run(scenario())

    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.FILLED
    # The signal's quantity at the tick's price — the two literals the harness
    # declares, not a number re-derived the way the ledger derives it.
    assert [
        (position.strategy_id, position.symbol, position.signed_size, position.entry_price)
        for position in store.all_positions()
    ] == [("trivial", "BTC", Decimal("0.5"), Decimal("42000"))]
    account = store.load_account()
    assert account is not None
    assert account.cash == GENESIS  # An opening fill realizes nothing.


def test_a_refused_ledger_write_leaves_neither_the_order_row_nor_the_ledger(
    tmp_path: Path,
) -> None:
    """Half a fill is the state the atomic write exists to make unreachable, so a
    write the store refuses must advance neither side of it (ADR-0043 §4).

    It must also raise ``InvariantViolation`` rather than let the driver's own
    error cross: that type is what pierces the engine's containment net and
    faults the run (ADR-0014), while a raw ``sqlite3.Error`` would be filed as a
    caller's bug and survived — the process running on with a ledger it can no
    longer make durable.

    A closed store stands in for any backend the write cannot reach. The saga is
    left in-flight first, so the store is alive for the write-ahead intent and
    dead only for the fill.
    """
    bus, _, store, _ = _harness(tmp_path / "saga.db")
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        # No tick cached: the send raises, leaving the saga in-flight with its
        # PENDING intent durable — then the venue's fill arrives against a store
        # that can no longer be written.
        with pytest.raises(ValueError):
            await bus.publish(_market_signal())
        store.close()
        with pytest.raises(InvariantViolation, match="ledger checkpoint write failed"):
            await bus.publish(_fill_report(cloid, trade_id="f1", quantity="0.5"))

    asyncio.run(scenario())

    with SQLiteStore(tmp_path / "saga.db") as reopened:
        record = reopened.get_order(cloid)
        assert record is not None
        assert record.state is OrderState.PENDING  # the fill never advanced it
        assert reopened.all_positions() == []
        assert reopened.load_account() is None


def test_a_refused_ledger_write_leaves_the_read_models_ahead_of_the_store(
    tmp_path: Path,
) -> None:
    """What the atomic write does *not* buy, pinned where the claim is easy to
    overstate: the durable record is all-or-nothing, the in-memory read-models
    are not.

    Both aggregates advance before the write is attempted, and they must —
    ``checkpoint_ledger`` takes the *folded* state as its input, so the fold
    cannot follow the write (ADR-0043 §4). ``Order.record_fill`` has likewise
    already advanced the saga the ``Cache`` holds by reference. A refused write
    therefore leaves both projections ahead of the store, and what makes that
    survivable is the ``InvariantViolation``: it pierces containment and faults
    the run (ADR-0014), so nothing goes on to trade or report against them.

    The saga is left partially filled first, so the refused fill is one the
    aggregates genuinely move on — a *first* fill would file its partition in
    ``project``, behind the write, and hide the divergence this pins.
    """
    wiring = _wiring(SQLiteStore(tmp_path / "saga.db"))
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await wiring.bus.publish(_tick("42000"))
        # A BUY LIMIT far below the market rests unfilled, so the saga stays
        # open across two partials rather than resolving on the first.
        await wiring.bus.publish(_limit_signal("1"))
        await wiring.bus.publish(_fill_report(cloid, trade_id="f1", quantity="0.4"))
        wiring.store.close()
        with pytest.raises(InvariantViolation):
            await wiring.bus.publish(_fill_report(cloid, trade_id="f2", quantity="0.4"))

    asyncio.run(scenario())

    order = wiring.cache.get_order(cloid)
    assert order is not None
    assert order.state is OrderState.FILLED  # the refused fill, readable in memory
    assert order.cum_qty == Decimal("0.8")
    position = wiring.portfolio.position("BTC", strategy_id="trivial")
    assert position is not None
    assert position.size == Decimal("0.8")

    with SQLiteStore(tmp_path / "saga.db") as reopened:
        record = reopened.get_order(cloid)
        assert record is not None
        assert record.state is OrderState.PARTIALLY_FILLED  # only the fill that landed
        assert record.cum_qty == Decimal("0.4")
        assert [stored.signed_size for stored in reopened.all_positions()] == [Decimal("0.4")]


class _StoreThatBreaksTheLedgerWrite(SQLiteStore):
    """The real store, except that the ledger write raises something the seam's
    error contract does not admit. Every other member is the real one, so the
    saga reaches its fill with the write-ahead intent durable."""

    def checkpoint_ledger(
        self,
        *,
        account: Account,
        positions: Sequence[Position] = (),
        order: Order | None = None,
        funding_mark: tuple[str, int] | None = None,
        ts_ns: int,
    ) -> None:
        raise RuntimeError("driver bug below the seam")


class _StoreThatBreaksTheOrderWrite(SQLiteStore):
    """The same contract break, on the narrow write every non-fill transition
    takes (ADR-0043 §4) — the other half of the manager's checkpoint surface."""

    def checkpoint(self, order: Order, *, ts_ns: int) -> None:
        raise RuntimeError("driver bug below the seam")


def test_a_broken_seam_contract_is_not_reported_as_a_failed_ledger_write() -> None:
    """``InvariantViolation`` is the whole of the ``Store`` seam's error contract
    (ADR-0019), and both adapters keep it through ``_durability``. Anything else
    crossing it is a bug *below* the seam, not a durability failure — so the
    manager must not relabel it as one: "ledger checkpoint write failed" is the
    one diagnosis that says the ledger did not move, and a store broken this way
    may well have moved it.

    The run faults either way — the manager's handlers are subscribed raw, so
    every exception reaches the runner. What the type decides is what the
    operator is told, not whether the engine survives.
    """
    wiring = _wiring(_StoreThatBreaksTheLedgerWrite(":memory:"))

    async def scenario() -> None:
        await wiring.bus.publish(_tick())
        with pytest.raises(RuntimeError, match="driver bug below the seam"):
            await wiring.bus.publish(_market_signal())

    asyncio.run(scenario())


def test_a_broken_seam_contract_is_not_reported_as_a_failed_checkpoint() -> None:
    """The non-fill half of the same rule. This path's wrapper spans
    ``Cache.checkpoint``, which writes the store and *then* projects — so a
    failure it did not narrow could report "checkpoint write failed" for a row
    that is already durable."""
    wiring = _wiring(_StoreThatBreaksTheOrderWrite(":memory:"))

    async def scenario() -> None:
        await wiring.bus.publish(_tick())
        with pytest.raises(RuntimeError, match="driver bug below the seam"):
            await wiring.bus.publish(_market_signal())

    asyncio.run(scenario())


def test_a_non_fill_transition_writes_the_order_row_and_no_ledger_row() -> None:
    """A fill is the only transition that moves the ledger, so every other one
    keeps the narrow ``Store.checkpoint`` (ADR-0043 §4).

    Not a redundant restatement of what the ledger holds: routing the whole saga
    through ``checkpoint_ledger`` would still pass every fill assertion in this
    file, while writing an account row on every ``PENDING`` intent and a position
    row for an order that never traded — a partition that reports a traded-flat
    record for a symbol nothing filled on.
    """
    bus, _, store, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        # A BUY LIMIT below the market rests unfilled: PENDING -> SUBMITTED ->
        # LIVE, three checkpoints and not one fill.
        await bus.publish(_limit_signal("41000"))

    asyncio.run(scenario())

    assert [type(ev) for ev in order_events] == [OrderPlaced, OrderSubmitted, OrderLive]
    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.LIVE
    assert store.all_positions() == []
    assert store.load_account() is None


def _fill_report(
    cloid: str, trade_id: str, quantity: str, *, ts_event: int = 1_000, fee: str = "0"
) -> FillReport:
    return FillReport(
        ts_event=ts_event,
        ts_init=ts_event,
        cloid=cloid,
        symbol="BTC",
        trade_id=trade_id,
        quantity=Decimal(quantity),
        price=Decimal("42000"),
        fee=Decimal(fee),
    )


def test_partial_fills_accumulate_to_filled_with_checkpoints() -> None:
    bus, _, store, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        # No tick cached: the send crashes, leaving the saga in-flight — then
        # the venue's fills arrive in two parts, as a live venue would push them.
        with pytest.raises(ValueError):
            await bus.publish(_market_signal())
        await bus.publish(_fill_report(cloid, trade_id="f1", quantity="0.2"))
        await bus.publish(_fill_report(cloid, trade_id="f2", quantity="0.3"))

    asyncio.run(scenario())

    fills = [ev for ev in order_events if isinstance(ev, OrderPartiallyFilled | OrderFilled)]
    assert [type(ev) for ev in fills] == [OrderPartiallyFilled, OrderFilled]
    assert [ev.cum_qty for ev in fills] == [Decimal("0.2"), Decimal("0.5")]

    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.FILLED
    assert record.cum_qty == Decimal("0.5")
    assert [state for state, _ in store.history(cloid)] == [
        OrderState.PENDING,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    ]


def test_each_fill_event_carries_the_fee_the_venue_reported_for_that_trade() -> None:
    # The venue is the fee's authority, so the manager propagates rather than
    # derives it — exactly as it already does for the report's ``ts_event`` and
    # ``reconciliation`` (ADR-0036). Per ``trade_id``, so two partials of one
    # order carry two independently reported fees and neither is a running total.
    bus, _, _, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        with pytest.raises(ValueError):
            await bus.publish(_market_signal())
        await bus.publish(_fill_report(cloid, trade_id="f1", quantity="0.2", fee="3.78"))
        await bus.publish(_fill_report(cloid, trade_id="f2", quantity="0.3", fee="5.67"))

    asyncio.run(scenario())

    fills = [ev for ev in order_events if isinstance(ev, OrderPartiallyFilled | OrderFilled)]
    assert [ev.fee for ev in fills] == [Decimal("3.78"), Decimal("5.67")]


def test_a_failed_checkpoint_write_is_an_invariant_violation() -> None:
    bus, _, store, _ = _harness()

    async def scenario() -> None:
        await bus.publish(_tick())
        # A dead store at checkpoint time must fail fast (ADR-0014): the saga
        # may not advance past a write it cannot make durable.
        store.close()
        with pytest.raises(InvariantViolation):
            await bus.publish(_market_signal())

    asyncio.run(scenario())


def test_time_passing_never_transitions_an_in_flight_saga() -> None:
    bus, clock, store, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        with pytest.raises(ValueError):
            await bus.publish(_market_signal())
        # An hour of dead air. A timeout is never a transition (ADR-0007 inv 1):
        # only a venue fact or reconciliation may move an in-flight saga.
        clock.advance_to(1_000 + 3_600_000_000_000)

    asyncio.run(scenario())

    assert store.history(cloid) == [(OrderState.PENDING, 1_000)]
    assert order_events == []


def test_place_signal_drives_placed_submitted_filled_in_order() -> None:
    bus, _, _, order_events = _harness()

    async def scenario() -> None:
        await bus.publish(_tick())
        await bus.publish(_market_signal())

    asyncio.run(scenario())

    assert [type(ev) for ev in order_events] == [OrderPlaced, OrderSubmitted, OrderFilled]


def test_cloid_is_derived_from_the_signal_id() -> None:
    bus, _, _, order_events = _harness()
    expected = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick())
        await bus.publish(_market_signal())

    asyncio.run(scenario())

    assert {ev.cloid for ev in order_events} == {expected}
    assert all(ev.signal_id == "trivial:BTC:1" for ev in order_events)


def test_order_filled_carries_the_fill_details() -> None:
    bus, _, _, order_events = _harness()

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_market_signal())

    asyncio.run(scenario())

    filled = next(ev for ev in order_events if isinstance(ev, OrderFilled))
    assert filled.price == Decimal("42000")
    assert filled.quantity == Decimal("0.5")
    assert filled.cum_qty == Decimal("0.5")
    assert filled.event_id == f"{filled.cloid}:fill:{filled.trade_id}"


def test_fill_preserves_the_venue_ts_event_while_ts_init_is_engine_time() -> None:
    bus, clock, _, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        # Leave the saga in-flight (no tick cached -> the send crashes), so the
        # order exists in cache to accept a hand-built venue fill.
        with pytest.raises(ValueError):
            await bus.publish(_market_signal())
        # The engine receives the fill strictly later than the venue stamped it.
        # On the paper path both clocks coincide, so the gap is only observable
        # by injecting a report whose ts_event predates the clock's current time.
        clock.advance_to(5_000)
        await bus.publish(
            FillReport(
                ts_event=2_000,  # venue fill instant, strictly earlier than now
                ts_init=2_000,
                cloid=cloid,
                symbol="BTC",
                trade_id="f1",
                quantity=Decimal("0.5"),
                price=Decimal("42000"),
            )
        )

    asyncio.run(scenario())

    filled = next(ev for ev in order_events if isinstance(ev, OrderFilled))
    assert filled.ts_event == 2_000  # venue fill instant preserved (when the fact occurred)
    assert filled.ts_init == 5_000  # engine construction time (clock at processing)


def test_partial_fill_ts_event_tracks_each_report_while_ts_init_is_engine_time() -> None:
    bus, clock, _, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        # In-flight saga, then two venue fills arriving after their own instants,
        # each received at a distinct (later) engine time.
        with pytest.raises(ValueError):
            await bus.publish(_market_signal())
        clock.advance_to(5_000)
        await bus.publish(_fill_report(cloid, trade_id="f1", quantity="0.2", ts_event=2_000))
        clock.advance_to(9_000)
        await bus.publish(_fill_report(cloid, trade_id="f2", quantity="0.3", ts_event=4_000))

    asyncio.run(scenario())

    fills = [ev for ev in order_events if isinstance(ev, OrderPartiallyFilled | OrderFilled)]
    assert [type(ev) for ev in fills] == [OrderPartiallyFilled, OrderFilled]
    # Each fill carries its own report's venue instant in ts_event...
    assert [ev.ts_event for ev in fills] == [2_000, 4_000]
    # ...while ts_init is engine construction time, monotonic in receipt order.
    assert [ev.ts_init for ev in fills] == [5_000, 9_000]


def test_duplicate_fill_report_yields_a_single_order_filled() -> None:
    bus, _, _, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick())
        await bus.publish(_market_signal())
        # Redeliver the exact fill (same trade_id -> same event_id): the saga is
        # already FILLED, so this must not publish a second OrderFilled.
        await bus.publish(
            FillReport(
                ts_event=1_000,
                ts_init=1_000,
                cloid=cloid,
                symbol="BTC",
                trade_id=f"{cloid}-1",
                quantity=Decimal("0.5"),
                price=Decimal("42000"),
            )
        )

    asyncio.run(scenario())

    filled = [ev for ev in order_events if isinstance(ev, OrderFilled)]
    assert len(filled) == 1
    assert filled[0].cum_qty == Decimal("0.5")


def test_fill_report_for_an_unknown_cloid_is_dropped() -> None:
    bus, _, _, order_events = _harness()

    async def scenario() -> None:
        # A fill for an order this manager never placed (reconciliation's concern
        # once it lands). It must be dropped silently: no OrderFilled, no raise.
        await bus.publish(
            FillReport(
                ts_event=1_000,
                ts_init=1_000,
                cloid="0xdeadbeef",
                symbol="BTC",
                trade_id="stray-1",
                quantity=Decimal("0.5"),
                price=Decimal("42000"),
            )
        )

    asyncio.run(scenario())

    assert order_events == []


def test_duplicate_signal_does_not_place_a_second_order() -> None:
    bus, _, _, order_events = _harness()

    async def scenario() -> None:
        await bus.publish(_tick())
        await bus.publish(_market_signal(seq=1))
        await bus.publish(_market_signal(seq=1))  # same signal_id -> same cloid

    asyncio.run(scenario())

    # Only the first signal produces a saga; the resent one is a no-op.
    assert [type(ev) for ev in order_events] == [OrderPlaced, OrderSubmitted, OrderFilled]


# --- LIMIT resting, cancel, and status handling (issue #13) -----------------


def test_resting_limit_drives_the_saga_to_live() -> None:
    bus, _, store, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        # A BUY LIMIT below the market rests: the venue reports it working, and
        # the manager drives PENDING -> SUBMITTED -> LIVE.
        await bus.publish(_limit_signal("41000"))

    asyncio.run(scenario())

    assert [type(ev) for ev in order_events] == [OrderPlaced, OrderSubmitted, OrderLive]
    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.LIVE
    assert [state for state, _ in store.history(cloid)] == [
        OrderState.PENDING,
        OrderState.SUBMITTED,
        OrderState.LIVE,
    ]


def test_duplicate_status_report_yields_a_single_order_live() -> None:
    bus, _, _, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("41000"))  # rests LIVE
        # Redeliver the venue's LIVE status (same {cloid}:LIVE event_id): the saga
        # already reflects it, so at-least-once delivery must not republish a
        # second OrderLive (ADR-0002 idempotency).
        await bus.publish(
            OrderStatusReport(
                ts_event=1_000,
                ts_init=1_000,
                cloid=cloid,
                symbol="BTC",
                status=OrderState.LIVE,
            )
        )

    asyncio.run(scenario())

    live = [ev for ev in order_events if isinstance(ev, OrderLive)]
    assert len(live) == 1


def test_resting_limit_fills_when_a_later_tick_crosses() -> None:
    bus, _, _, order_events = _harness()

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("41000"))  # rests LIVE
        await bus.publish(_tick("41000"))  # crosses -> fills

    asyncio.run(scenario())

    assert [type(ev) for ev in order_events] == [
        OrderPlaced,
        OrderSubmitted,
        OrderLive,
        OrderFilled,
    ]


def test_post_only_that_would_cross_is_rejected_with_the_venue_reason() -> None:
    bus, _, store, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        # post_only BUY LIMIT above the market would cross on arrival -> rejected.
        await bus.publish(_limit_signal("43000", post_only=True))

    asyncio.run(scenario())

    rejected = [ev for ev in order_events if isinstance(ev, OrderRejected)]
    assert len(rejected) == 1
    assert rejected[0].reason == "post_only order would cross"
    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.REJECTED


def test_unfilled_ioc_limit_cancels_straight_from_submitted() -> None:
    bus, _, store, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("41000", time_in_force=TimeInForce.IOC))

    asyncio.run(scenario())

    # Never LIVE: an unfilled IOC goes SUBMITTED -> CANCELLED.
    assert [type(ev) for ev in order_events] == [OrderPlaced, OrderSubmitted, OrderCancelled]
    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.CANCELLED


def test_cancel_signal_checkpoints_the_marker_and_cancels_the_derived_cloid() -> None:
    bus, _, store, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("41000"))  # rests LIVE
        # The strategy cancels by the signal_id it emitted; the manager re-derives
        # the cloid, marks intent durably, and the venue confirms the cancel.
        await bus.publish(_cancel_signal("trivial:BTC:1"))

    asyncio.run(scenario())

    assert isinstance(order_events[-1], OrderCancelled)
    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.CANCELLED
    # The cancel_requested marker was checkpointed (durable before the send),
    # including the cancel's own signal_id for the seq high-water-mark (ADR-0026).
    assert record.cancel_requested is True
    assert record.cancel_requested_ts is not None
    assert record.cancel_signal_id == "trivial:BTC:2"


def test_a_venue_fill_wins_the_race_and_a_later_cancel_is_a_no_op() -> None:
    bus, _, store, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("41000"))  # rests LIVE
        # The venue fills the resting order (pushes the fill) before our cancel
        # lands: FILLED is terminal and wins (ADR-0026).
        await bus.publish(_fill_report(cloid, trade_id="v1", quantity="0.5"))
        await bus.publish(_cancel_signal("trivial:BTC:1"))

    asyncio.run(scenario())

    assert isinstance(order_events[-1], OrderFilled)
    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.FILLED  # the late cancel did not move it


def test_a_late_cancel_ack_on_a_terminal_saga_is_an_idempotent_no_op() -> None:
    bus, _, store, order_events = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("41000"))  # rests LIVE
        # The fill wins the cancel/fill race: FILLED, terminal (ADR-0026).
        await bus.publish(_fill_report(cloid, trade_id="v1", quantity="0.5"))
        # The venue's ack for the cancel that lost arrives late. A terminal
        # saga absorbs it silently — routing it into Order.apply would raise
        # InvariantViolation on FILLED -> CANCELLED (ADR-0026; #13 R001).
        await bus.publish(
            OrderStatusReport(
                ts_event=2_000,
                ts_init=2_000,
                cloid=cloid,
                symbol="BTC",
                status=OrderState.CANCELLED,
                reason="cancel acknowledged",
            )
        )

    asyncio.run(scenario())

    assert isinstance(order_events[-1], OrderFilled)
    assert not [ev for ev in order_events if isinstance(ev, OrderCancelled)]
    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.FILLED


def test_cancel_signal_for_an_unknown_order_is_a_benign_no_op() -> None:
    bus, _, _, order_events = _harness()

    async def scenario() -> None:
        await bus.publish(_tick("42000"))
        # No order was ever placed for this target: nothing to cancel, nothing raised.
        await bus.publish(_cancel_signal("trivial:BTC:999"))

    asyncio.run(scenario())

    assert order_events == []


async def _record(sink: list, event: object) -> None:
    sink.append(event)


def test_a_restart_rebuilt_cache_dedups_a_redelivered_place_signal() -> None:
    # First life: a GTC LIMIT rests on the book — a durable non-terminal saga.
    bus, _, store, _ = _harness()
    cloid = derive_cloid("trivial:BTC:1")

    async def first_life() -> None:
        await bus.publish(_tick("42000"))
        await bus.publish(_limit_signal("41000"))

    asyncio.run(first_life())
    record = store.get_order(cloid)
    assert record is not None and record.state is OrderState.LIVE

    # Crash: every in-memory component dies; only the store survives. The
    # second life rebuilds the projection from it (ADR-0009 recovery step 2).
    bus2 = InMemoryBus()
    clock2 = ManualClock(start_ns=2_000)
    exchange2 = PaperExchange(
        bus=bus2, clock=clock2, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
    )
    checkpointer2 = checkpointer(store, clock=clock2)
    checkpointer2.recover()
    manager2 = ExecutionManager(bus=bus2, exchange=exchange2, checkpointer=checkpointer2)
    bus2.subscribe(Signal, manager2.on_signal)
    bus2.subscribe(ExecutionReport, manager2.on_execution_report)
    second_life_events: list[OrderEvent] = []
    bus2.subscribe(OrderEvent, lambda ev: _record(second_life_events, ev))
    history_before = store.history(cloid)

    # At-least-once redelivery of the same signal across the restart: the
    # rebuilt projection recognizes the cloid — never a second placement.
    asyncio.run(bus2.publish(_limit_signal("41000")))

    assert second_life_events == []
    assert store.history(cloid) == history_before


def _stochastic_harness(
    fill_model: object,
) -> tuple[InMemoryBus, ManualClock, SQLiteStore, list[OrderEvent]]:
    bus = InMemoryBus()
    clock = ManualClock(start_ns=1_000)
    store = SQLiteStore(":memory:")
    exchange = PaperExchange(
        bus=bus,
        clock=clock,
        fill_model=fill_model,  # type: ignore[arg-type]
        genesis_collateral=GENESIS,
    )
    manager = ExecutionManager(
        bus=bus, exchange=exchange, checkpointer=checkpointer(store, clock=clock)
    )

    bus.subscribe(Signal, manager.on_signal)
    bus.subscribe(ExecutionReport, manager.on_execution_report)

    order_events: list[OrderEvent] = []
    bus.subscribe(OrderEvent, lambda ev: _record(order_events, ev))
    return bus, clock, store, order_events


def test_stochastic_partials_drive_the_saga_from_live_to_filled() -> None:
    """The acceptance criterion end-to-end: a resting GTC LIMIT that the
    StochasticFillModel partial-fills across crossing ticks drives the real
    saga LIVE → PARTIALLY_FILLED* → FILLED, with a monotonic cum_qty that
    converges to exactly the order size — no report hand-fed, the paper venue
    and the seeded model produce every fill."""
    model = StochasticFillModel(
        rng=random.Random(0),
        clock=ManualClock(),
        params=StochasticParams(prob_fill_on_limit=1.0, partial_fill_fraction=Decimal("0.4")),
    )
    bus, _, store, order_events = _stochastic_harness(model)
    cloid = derive_cloid("trivial:BTC:1")

    async def scenario() -> None:
        await bus.publish(_tick("42000"))  # above the limit: the order rests
        await bus.publish(_limit_signal("41000"))  # GTC BUY, qty 0.5, rests LIVE
        for _ in range(3):
            await bus.publish(_tick("41000"))  # crossing ticks: 0.2, 0.2, 0.1

    asyncio.run(scenario())

    fills = [ev for ev in order_events if isinstance(ev, OrderPartiallyFilled | OrderFilled)]
    assert [type(ev) for ev in fills] == [
        OrderPartiallyFilled,
        OrderPartiallyFilled,
        OrderFilled,
    ]
    cum = [ev.cum_qty for ev in fills]
    assert cum == sorted(cum)  # monotonic
    assert cum == [Decimal("0.2"), Decimal("0.4"), Decimal("0.5")]

    record = store.get_order(cloid)
    assert record is not None
    assert record.state is OrderState.FILLED
    assert record.cum_qty == Decimal("0.5")
    assert [state for state, _ in store.history(cloid)] == [
        OrderState.PENDING,
        OrderState.SUBMITTED,
        OrderState.LIVE,
        OrderState.PARTIALLY_FILLED,
        OrderState.PARTIALLY_FILLED,
        OrderState.FILLED,
    ]
