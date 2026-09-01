"""``LedgerReconciliation`` — the account-grain cross-check (ADR-0034/0040).

The live-only cadence anchored on one ``fetch_account_state()`` read: it
classifies what the ledger and the venue disagree about, by tier, and **changes
no stored value** in this slice. The heals land on the classification it
establishes.

Wired the way the runner wires it — a real ``PortfolioProjection`` over a real
``SQLiteStore``, a ``ManualClock``, an ``InMemoryBus`` — against a venue double
answering with recorded venue figures, which is the only half a test may invent:
the point of the cross-check is that the second number comes from somewhere the
projection did not compute it.
"""

import asyncio
from decimal import Decimal

from ledgers import book_fill
from venue_doubles import DERIVED_GENESIS, DERIVED_STATE, LIVE_ACCOUNT_ID, LiveVenueDouble

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AccountSpec,
    MarkTick,
    OrderFilled,
    OrderFillEvent,
    PlaceOrder,
    Side,
    VenueAccountState,
    VenueOrderView,
    VenueReadFailure,
)
from tickwright.engine.ledger_reconcile import (
    Divergence,
    DivergenceQuantity,
    DivergenceTier,
    LedgerReconcileConfig,
    LedgerReconciliation,
)
from tickwright.engine.portfolio import PortfolioProjection
from tickwright.observability.testing import capture_events

_ENTRY = "64809.0"
"""The recorded snapshot's own entry price — see ``venue_doubles.account_state``."""

_AGREEING_MARK = "64792.0"
"""The mark at which the projection's uPnL on 0.002 BTC long reads the venue's
own −0.034: ``(64792.0 − 64809.0) × 0.002``. Derived from the recorded figures
rather than from the code under test, so the Tier-2 legs have an independent
expected value."""


def _live_ledger(store: SQLiteStore, clock: ManualClock) -> PortfolioProjection:
    """A ledger on the **live** shape — an ingested genesis, so the opening cash
    line is the venue's number and not a declaration a test chose."""
    return PortfolioProjection(
        spec=AccountSpec(account_id=LIVE_ACCOUNT_ID, genesis_collateral=None),
        store=store,
        clock=clock,
    )


def _fill(
    *,
    quantity: str,
    price: str,
    symbol: str = "BTC",
    strategy_id: str = "alpha",
    fee: str = "0",
) -> OrderFillEvent:
    return OrderFilled(
        ts_event=1_000,
        ts_init=1_000,
        cloid=f"0x{strategy_id}-{symbol}-{quantity}",
        strategy_id=strategy_id,
        signal_id=f"{strategy_id}:{symbol}:1",
        symbol=symbol,
        trade_id=f"t-{strategy_id}-{symbol}-{quantity}",
        quantity=Decimal(quantity),
        price=Decimal(price),
        cum_qty=Decimal(quantity),
        fee=Decimal(fee),
    )


def _mark(price: str, *, symbol: str = "BTC") -> MarkTick:
    return MarkTick(ts_event=2_000, ts_init=2_000, symbol=symbol, price=Decimal(price))


def _agreeing_ledger(store: SQLiteStore, clock: ManualClock) -> PortfolioProjection:
    """A ledger holding exactly what ``DERIVED_STATE`` reports: 0.002 BTC long
    off an ingested genesis, marked so its uPnL matches the venue's."""
    projection = _live_ledger(store, clock)
    projection.materialise(DERIVED_STATE)
    book_fill(projection, _fill(quantity="0.002", price=_ENTRY), side=Side.BUY)
    projection.observe_mark(_mark(_AGREEING_MARK))
    return projection


class _ReadOnlyVenue(LiveVenueDouble):
    """A live venue the account cycle may only *read*.

    The three order-path members carry this suite's meaning by refusing: the
    cycle's whole contract this slice is that it classifies and heals nothing, so
    a placement reaching the venue is the failure, not an unimplemented member.
    """

    async def place(self, order: PlaceOrder) -> None:
        raise AssertionError("the account cycle places nothing: it classifies, it never heals")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("the account cycle cancels nothing: it classifies, it never heals")

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        raise AssertionError("the account cycle is anchored on the account read, never on a cloid")


def _cycle(
    projection: PortfolioProjection, venue: _ReadOnlyVenue, clock: ManualClock
) -> LedgerReconciliation:
    return LedgerReconciliation(
        exchange=venue,
        portfolio=projection,
        clock=clock,
        bus=InMemoryBus(),
        config=LedgerReconcileConfig(),
    )


def _tier_1(divergences: tuple[Divergence, ...] | None) -> list[Divergence]:
    """The Tier-1 verdicts alone, for a case whose subject is the ledger half.

    A ``None`` here would be a freeze, and letting it read as "no Tier-1
    divergence" is the collapse the return type exists to prevent — so it fails
    loudly rather than filtering to an empty list."""
    assert divergences is not None, "the cycle froze; this case expects a completed comparison"
    return [item for item in divergences if item.tier is DivergenceTier.TIER_1]


def _tier_2(divergences: tuple[Divergence, ...] | None) -> list[Divergence]:
    """The Tier-2 verdicts alone, sorted, for a case whose subject is the
    computed half. Sorted because the two grains are collected independently and
    their order carries no meaning a case should be pinned to."""
    assert divergences is not None, "the cycle froze; this case expects a completed comparison"
    return sorted(
        (item for item in divergences if item.tier is DivergenceTier.TIER_2),
        key=lambda item: item.quantity,
    )


def test_a_cycle_agreeing_with_the_venue_reports_no_divergence() -> None:
    """The tracer: one venue read, nothing to classify, and a record saying so.

    ``()`` is not ``None`` — an agreeing cycle and a frozen one are different
    outcomes, and collapsing them would let an outage read as a clean book."""
    clock = ManualClock(start_ns=1_000)
    store = SQLiteStore(":memory:")
    venue = _ReadOnlyVenue()
    reconciliation = _cycle(_agreeing_ledger(store, clock), venue, clock)

    with capture_events() as logs:
        divergences = asyncio.run(reconciliation.reconcile_account())

    assert divergences == ()
    assert venue.account_reads == 1
    assert [str(log["event"]) for log in logs] == ["account.reconciled"]


def test_a_net_size_the_venue_does_not_hold_is_tier_1() -> None:
    """The Σ-invariant's venue link: the comparison is the account net over
    **every** partition against the venue's one position, so two strategies
    holding the same symbol are one number here (ADR-0034/0041 §4). Split per
    partition it would disagree with a venue that has no per-strategy truth.

    Only the Tier-1 verdicts are asserted: the same drifted book also moves the
    computed valuations, which is Tier-2's subject rather than this one's."""
    clock = ManualClock(start_ns=1_000)
    projection = _live_ledger(SQLiteStore(":memory:"), clock)
    projection.materialise(DERIVED_STATE)
    book_fill(projection, _fill(quantity="0.002", price=_ENTRY), side=Side.BUY)
    book_fill(projection, _fill(quantity="0.001", price=_ENTRY, strategy_id="beta"), side=Side.BUY)
    projection.observe_mark(_mark(_AGREEING_MARK))

    divergences = asyncio.run(_cycle(projection, _ReadOnlyVenue(), clock).reconcile_account())

    assert _tier_1(divergences) == [
        Divergence(
            tier=DivergenceTier.TIER_1,
            quantity=DivergenceQuantity.SIGNED_SIZE,
            symbol="BTC",
            projected=Decimal("0.003"),
            venue=Decimal("0.002"),
        )
    ]


def test_a_cash_line_the_venue_does_not_back_is_tier_1() -> None:
    """The account grain's Tier-1 leg, against ``equity − Σ unrealized_pnl``.

    The subtraction is the comparison's whole content: the venue's equity already
    contains its unrealized PnL, so comparing our cash line straight against
    equity would report a divergence on every account holding a position
    (ADR-0042 §6). ``symbol`` is ``None`` because there is one collateral pool
    per process and no symbol owns it (ADR-0038)."""
    clock = ManualClock(start_ns=1_000)
    projection = _live_ledger(SQLiteStore(":memory:"), clock)
    projection.materialise(DERIVED_STATE)
    # A fee the venue's own figures never charged: the size still agrees, so the
    # cash leg is the only thing this case can be reading.
    book_fill(projection, _fill(quantity="0.002", price=_ENTRY, fee="0.5"), side=Side.BUY)
    projection.observe_mark(_mark(_AGREEING_MARK))

    divergences = asyncio.run(_cycle(projection, _ReadOnlyVenue(), clock).reconcile_account())

    assert _tier_1(divergences) == [
        Divergence(
            tier=DivergenceTier.TIER_1,
            quantity=DivergenceQuantity.CASH,
            symbol=None,
            projected=Decimal("25.4604"),  # the ingested 25.9604 genesis, less the fee
            venue=DERIVED_GENESIS,
        )
    ]


class _BlippingVenue(_ReadOnlyVenue):
    """A venue whose first account read fails and whose second succeeds.

    The outage that must not read as a flat book, followed by the recovery that
    must not need a restart — one double, because the pair is the behaviour."""

    def __init__(self) -> None:
        super().__init__()
        self._answers: list[VenueAccountState | None] = [None, DERIVED_STATE]

    async def fetch_account_state(self) -> VenueAccountState | None:
        self.account_reads += 1
        return self._answers.pop(0)


def test_an_unanswered_read_freezes_the_cycle_and_the_next_one_recovers() -> None:
    """ADR-0011 inv 1 at the account grain: an outage is never a flat book.

    Nothing is classified — a comparison against an absent venue would report
    the whole book as divergent — and nothing is removed, so the position and
    the cash line stand untouched for the next cycle to compare. The freeze is
    **recorded**, because a cycle that heals nothing and says nothing is
    indistinguishable from a cadence that stopped running.

    The recovery needs no restart: the very next read is compared normally."""
    clock = ManualClock(start_ns=1_000)
    venue = _BlippingVenue()
    reconciliation = _cycle(_agreeing_ledger(SQLiteStore(":memory:"), clock), venue, clock)

    with capture_events() as logs:
        frozen = asyncio.run(reconciliation.reconcile_account())
    assert frozen is None
    assert [str(log["event"]) for log in logs] == ["account.reconcile_frozen"]
    assert [log["step"] for log in logs] == ["cadence"]

    with capture_events() as logs:
        recovered = asyncio.run(reconciliation.reconcile_account())
    assert recovered == ()
    assert [str(log["event"]) for log in logs] == ["account.reconciled"]
    assert venue.account_reads == 2


def test_a_valuation_the_venue_computes_differently_is_tier_2() -> None:
    """The computed half, on a book whose Tier-1 legs agree exactly.

    A stale mark moves every mark-dependent number and nothing else, so the
    ledger is *correct* and only its valuation is behind — which is the whole
    reason this tier alerts rather than heals (ADR-0034). Both grains are
    compared: the account's equity and the position's own unrealized PnL."""
    clock = ManualClock(start_ns=1_000)
    projection = _live_ledger(SQLiteStore(":memory:"), clock)
    projection.materialise(DERIVED_STATE)
    book_fill(projection, _fill(quantity="0.002", price=_ENTRY), side=Side.BUY)
    # $100 behind the venue's own mark, against the $17 skew its figures imply.
    projection.observe_mark(_mark("64709.0"))

    divergences = asyncio.run(_cycle(projection, _ReadOnlyVenue(), clock).reconcile_account())

    assert _tier_1(divergences) == []
    assert _tier_2(divergences) == [
        Divergence(
            tier=DivergenceTier.TIER_2,
            quantity=DivergenceQuantity.EQUITY,
            symbol=None,
            projected=Decimal("25.7604"),  # the 25.9604 cash line, less our own uPnL
            venue=Decimal("25.9264"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_2,
            quantity=DivergenceQuantity.UNREALIZED_PNL,
            symbol="BTC",
            projected=Decimal("-0.2"),  # (64709.0 − 64809.0) × 0.002
            venue=Decimal("-0.034"),
        ),
    ]


def _durable_ledger(store: SQLiteStore) -> tuple[object, ...]:
    """Everything the ledger has made durable, in a form two reads can compare.

    ``Account`` is a mutable identity type with no ``__eq__``, so comparing two
    reads of it straight would compare object identity and pass through any
    write — it is read out field by field instead. ``Position`` is a dataclass
    and compares by value, so the partitions go in whole.
    """
    account = store.load_account()
    assert account is not None, "the ledger was never opened: this case would guard nothing"
    return (
        account.account_id,
        account.genesis_collateral,
        account.genesis_ts_ns,
        account.cash,
        tuple(store.all_positions()),
    )


def test_a_cycle_over_a_diverging_book_changes_no_stored_value() -> None:
    """The slice's contract where it actually binds: classify, heal nothing.

    Asserted against the **store** rather than against the return value, because
    the two say different things — a cycle that reported exactly the right
    divergences and also wrote one of them back would satisfy every case above
    this one. The heals land on this classification in later slices, so what
    keeps them honest is a case that reads the durable side.

    The book diverges on every leg the cycle compares — a phantom the venue is
    flat in, foreign flow the ledger never placed, a fee the venue's figures
    never charged, and the valuations all of that moves — so no comparison path
    is skipped before the store is read back.

    The positions table is empty on both reads, and that is a guard rather than a
    gap: this suite's ``book_fill`` deliberately omits the durable write
    (asserted at the ``Checkpointer``'s own seam), while the venue *does* report
    a BTC position the ledger has never held. A cycle that materialised foreign
    flow would put a row there.
    """
    clock = ManualClock(start_ns=1_000)
    store = SQLiteStore(":memory:")
    projection = _live_ledger(store, clock)
    projection.materialise(DERIVED_STATE)
    book_fill(projection, _fill(quantity="5", price="100", symbol="SOL", fee="0.5"), side=Side.BUY)
    projection.observe_mark(_mark("90", symbol="SOL"))
    before = _durable_ledger(store)

    divergences = asyncio.run(_cycle(projection, _ReadOnlyVenue(), clock).reconcile_account())

    assert divergences, "vacuous unless the cycle found something a later slice would heal"
    assert _durable_ledger(store) == before


def test_a_symbol_only_one_side_holds_is_still_compared() -> None:
    """Neither side's symbol set bounds the comparison.

    A symbol the ledger holds and the venue does not is a phantom position; one
    the venue holds and the ledger does not is foreign flow the engine never
    placed. Both are Tier-1, and a comparison walking only our own book would
    see the second as nothing at all — which is precisely the drift the venue
    link exists to catch."""
    clock = ManualClock(start_ns=1_000)
    projection = _live_ledger(SQLiteStore(":memory:"), clock)
    projection.materialise(DERIVED_STATE)
    book_fill(projection, _fill(quantity="5", price="100", symbol="SOL"), side=Side.BUY)

    divergences = asyncio.run(_cycle(projection, _ReadOnlyVenue(), clock).reconcile_account())

    assert sorted(_tier_1(divergences), key=lambda item: str(item.symbol)) == [
        Divergence(
            tier=DivergenceTier.TIER_1,
            quantity=DivergenceQuantity.SIGNED_SIZE,
            symbol="BTC",  # foreign flow: the venue holds it, the ledger does not
            projected=Decimal("0"),
            venue=Decimal("0.002"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_1,
            quantity=DivergenceQuantity.SIGNED_SIZE,
            symbol="SOL",  # a phantom: the ledger holds it, the venue does not
            projected=Decimal("5"),
            venue=Decimal("0"),
        ),
    ]


# --- The barrier's account step ----------------------------------------------
#
# The startup materialisation is this type's *other* account read, and it lives
# here rather than on the runner for the reason #191 deferred to this slice: the
# barrier composes two bound methods on two grain owners, and until this module
# existed the account step had no owner to be a method of.


def test_an_unanswered_barrier_read_is_recorded_as_a_freeze() -> None:
    """The account grain's other freeze, and until now the silent one.

    The barrier's account step used to return a bare ``False`` and emit nothing,
    so an operator whose startup faulted on the account read got
    ``engine.faulted`` and no record naming the read that caused it — while the
    identical freeze one step later was fully recorded (#191 handover, item 2).

    Both carry the one name now, told apart by ``step`` rather than by a second
    name, because the grain and the cause are identical and nothing routes on the
    difference. What differs is the **cost**, and that an operator does need told
    apart: this freeze faults the process once the barrier's retry budget runs
    out, where the cadence's skips a single cycle.
    """
    clock = ManualClock(start_ns=1_000)
    venue = _ReadOnlyVenue(state=None)
    reconciliation = _cycle(_live_ledger(SQLiteStore(":memory:"), clock), venue, clock)

    with capture_events() as logs:
        opened = asyncio.run(reconciliation.materialise_account())

    assert opened is False
    assert venue.account_reads == 1
    assert [str(log["event"]) for log in logs] == ["account.reconcile_frozen"]
    assert [log["step"] for log in logs] == ["barrier"]
