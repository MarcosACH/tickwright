"""``LedgerReconciliation`` — the account-grain cycle that classifies and heals
nothing (issue #193).

Exercised through its public verbs against a **live**-shaped ledger over an
in-memory store and a venue double answering recorded account snapshots. The
venue is doubled because it is a process boundary (ADR-0022) and it is the only
thing doubled here: the ledger, its store and its clock are the real ones, so a
case asserts what the cycle *concludes* about a book a fill actually moved.
"""

import asyncio
from dataclasses import replace
from decimal import Decimal

from ledgers import book_fill
from venue_doubles import LIVE_ACCOUNT_ID, LiveVenueDouble, account_state

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AccountSpec,
    MarkTick,
    OrderFilled,
    PlaceOrder,
    Side,
    Store,
    VenueAccountState,
    VenueOrderView,
    VenueReadFailure,
)
from tickwright.engine.ledger_reconcile import (
    Divergence,
    DivergenceTier,
    LedgerReconciliation,
)
from tickwright.engine.portfolio import PortfolioProjection
from tickwright.observability import NamedEvent
from tickwright.observability.testing import capture_events


class _AccountVenue(LiveVenueDouble):
    """A live venue that answers the account read and nothing else.

    The three members ``VenueDouble`` deliberately withholds carry this suite's
    meaning: the account cycle is anchored on one account snapshot, so a cloid
    read or an order action reaching the seam is the specification being broken,
    not a case needing another stub.

    ``answers`` is consumed one per cycle and the last one repeats, so a case
    can put a **failed** read in front of a good one and assert the cadence
    recovers at its next deadline rather than staying frozen — a distinction a
    double answering one fixed value could not express.
    """

    def __init__(self, *answers: VenueAccountState | None) -> None:
        super().__init__(state=answers[0])
        self._answers = list(answers)

    async def fetch_account_state(self) -> VenueAccountState | None:
        self.account_reads += 1
        return self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]

    async def place(self, order: PlaceOrder) -> None:
        raise AssertionError("the account cycle places nothing")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("the account cycle cancels nothing")

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        raise AssertionError("the account cycle is anchored on the account snapshot, not a cloid")


def _ledger(store: Store, *, equity: str) -> PortfolioProjection:
    """A live ledger already materialised, the state every cycle finds it in.

    Live is the only shape that has a cycle at all — paper has no second account
    to compare against — so the genesis is ``None`` (ingested, ADR-0042 §6) and
    the opening cash line is the venue's own, taken from a snapshot holding no
    position so that ``equity`` *is* the cash a case declares.
    """
    projection = PortfolioProjection(
        spec=AccountSpec(account_id=LIVE_ACCOUNT_ID, genesis_collateral=None),
        store=store,
        clock=ManualClock(7),
    )
    projection.recover()
    projection.materialise(account_state(equity))
    return projection


def _book_fill(
    projection: PortfolioProjection,
    *,
    quantity: str,
    price: str,
    symbol: str = "BTC",
    strategy_id: str = "alpha",
) -> None:
    """Fold one filled buy into the ledger — the only way a partition exists.

    A case that wants the cycle to look at a *position* books one rather than
    writing the row itself: what the venue is compared against has to be a book
    the fill path actually produced.
    """
    book_fill(
        projection,
        OrderFilled(
            ts_event=1_000,
            ts_init=1_000,
            cloid=f"0x{strategy_id}-{symbol}",
            strategy_id=strategy_id,
            signal_id=f"{strategy_id}:{symbol}:1",
            symbol=symbol,
            trade_id=f"{symbol}-1",
            quantity=Decimal(quantity),
            price=Decimal(price),
            cum_qty=Decimal(quantity),
            fee=Decimal("0"),
        ),
        side=Side.BUY,
    )


def test_a_cycle_whose_snapshot_matches_the_ledger_reports_no_divergence() -> None:
    """The agreeing pass, and the anchor it rests on: **one** account read is the
    whole cycle's venue cost (ADR-0034), so the read count is also the assertion
    that nothing polls per symbol.

    An empty tuple is a book that agreed, and it is deliberately not the same
    answer as the freeze below: an outage is never a flat book (ADR-0011 inv 1).
    """
    store = SQLiteStore(":memory:")
    projection = _ledger(store, equity="100000")
    venue = _AccountVenue(account_state("100000"))
    cycle = LedgerReconciliation(exchange=venue, portfolio=projection)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == ()
    assert venue.account_reads == 1
    assert [log["event"] for log in logs] == [NamedEvent.ACCOUNT_RECONCILED.value]
    assert projection.account().cash == Decimal("100000")


def test_a_failed_account_read_freezes_the_cycle_and_leaves_the_book_alone() -> None:
    """The anchor failed, so there is nothing to reconcile *against* — and the
    one answer that must never be inferred from it is a flat book (ADR-0011
    inv 1). ``None`` is that freeze, distinct from the empty tuple above.

    A frozen cycle is also not a stuck one: the next deadline reads again, and
    a venue that has come back reconciles normally. The freeze costs one cycle,
    which is exactly why it is recorded under its own name rather than left as
    the silent early return an operator would have to infer from the gap in
    ``account.reconciled``.
    """
    store = SQLiteStore(":memory:")
    projection = _ledger(store, equity="100000")
    _book_fill(projection, quantity="0.002", price="64809")
    held = projection.open_positions(strategy_id="alpha")
    cash = projection.account().cash
    venue = _AccountVenue(None, account_state("100000"))
    cycle = LedgerReconciliation(exchange=venue, portfolio=projection)

    with capture_events() as logs:
        frozen = asyncio.run(cycle.reconcile_account())

    assert frozen is None
    assert [log["event"] for log in logs] == [NamedEvent.ACCOUNT_RECONCILE_FROZEN.value]
    assert projection.open_positions(strategy_id="alpha") == held
    assert projection.account().cash == cash

    assert asyncio.run(cycle.reconcile_account()) is not None  # the venue came back
    assert venue.account_reads == 2


def _held(equity: str, *positions: tuple[str, str, str]) -> VenueAccountState:
    """A venue snapshot holding an explicit ``(symbol, signed_size, uPnL)`` per entry.

    ``account_state`` answers the recorded BTC snapshot, which is the right
    shape for a materialisation and the wrong one for a *divergence*: a
    disagreement is a claim about which symbols each side holds and at what
    size, so the sizes have to be the case's own. Everything not compared stays
    the recorded snapshot's figures — what the cycle is handed is still a shape
    the venue could have returned.
    """
    recorded = account_state(equity, "-0.034").positions[0]
    return VenueAccountState(
        equity=Decimal(equity),
        free_margin=Decimal("0.0096"),
        cross_maintenance_margin=Decimal("1.6198"),
        positions=tuple(
            replace(
                recorded,
                symbol=symbol,
                signed_size=Decimal(size),
                unrealized_pnl=Decimal(unrealized),
            )
            for symbol, size, unrealized in positions
        ),
    )


def test_a_symbol_whose_ledger_net_disagrees_with_the_venue_size_diverges_at_tier_1() -> None:
    """Signed size is Tier-1 — accumulated, so zero economic tolerance: any gap
    is a missed or duplicated fill, never noise (ADR-0034).

    The comparison ranges over the **union** of both symbol sets, and the three
    ways a symbol lands in it are the whole behavior. A symbol both sides hold
    at different sizes is the obvious one. A symbol only the *ledger* holds is a
    venue that has it flat — comparing only what the venue returned would drop
    the position silently, which is a book we believe we hold and do not. A
    symbol only the *venue* holds is foreign flow the engine never placed
    (ADR-0038's unattributed partition) — comparing only what the ledger knows
    about would never see it at all.

    A symbol both sides agree on is in the union too and yields nothing: what
    the cycle reports is the disagreements, not the roster.
    """
    store = SQLiteStore(":memory:")
    projection = _ledger(store, equity="100000")
    _book_fill(projection, quantity="0.002", price="64809")
    _book_fill(projection, quantity="1.5", price="3000", symbol="ETH")
    _book_fill(projection, quantity="100", price="0.4", symbol="DOGE")
    venue = _held(
        "100000",
        ("BTC", "0.003", "0"),  # a fill the ledger missed
        ("DOGE", "100", "0"),  # agrees
        ("SOL", "10", "0"),  # flow the engine never placed
    )  # ETH: held by the ledger, flat at the venue
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), portfolio=projection)

    divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == (
        Divergence(
            tier=DivergenceTier.TIER_1,
            field="signed_size",
            symbol="BTC",
            ledger=Decimal("0.002"),
            venue=Decimal("0.003"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_1,
            field="signed_size",
            symbol="ETH",
            ledger=Decimal("1.5"),
            venue=Decimal("0"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_1,
            field="signed_size",
            symbol="SOL",
            ledger=Decimal("0"),
            venue=Decimal("10"),
        ),
    )


def test_a_cash_line_disagreeing_with_the_equity_the_venue_implies_diverges_at_tier_1() -> None:
    """Cash is Tier-1 and the venue reports no cash line, so the comparison is
    against the one its snapshot *implies*: ``equity − Σ unrealized_pnl``.

    The subtraction is the whole point. Equity already contains unrealized PnL
    (ADR-0040 §7's ``equity = cash + Σ uPnL``), so comparing the ledger's cash
    against the venue's equity directly would read a divergence of exactly the
    open uPnL on every account holding a position — a permanent false positive
    that the next slice's heal would then chase, moving a correct cash line
    toward a figure no venue holds.

    Here the venue's 100300 equity carries 500 of open profit, so the cash
    behind it is 99800 against the ledger's 100000: a real 200 gap, and one the
    un-subtracted comparison would have reported as 300.

    The position sizes agree, so the cycle's only finding is the account-grain
    line — cash carries no symbol, because the account has one collateral pool
    and nothing to attribute it to.
    """
    store = SQLiteStore(":memory:")
    projection = _ledger(store, equity="100000")
    _book_fill(projection, quantity="0.002", price="64809")
    venue = _held("100300", ("BTC", "0.002", "500"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), portfolio=projection)

    divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == (
        Divergence(
            tier=DivergenceTier.TIER_1,
            field="cash",
            symbol=None,
            ledger=Decimal("100000"),
            venue=Decimal("99800"),
        ),
    )


def _mark(projection: PortfolioProjection, symbol: str, price: str) -> None:
    """Feed the projection the Tier-2 valuation input (ADR-0039).

    A Tier-2 case has to go through this rather than assert on a figure it
    passed in: uPnL and equity are *recomputed on every read* from the cached
    mark, so a case that never observed one is asserting on the ``None`` the
    absence produces, not on a valuation.
    """
    projection.observe_mark(
        MarkTick(ts_event=2_000, ts_init=2_000, symbol=symbol, price=Decimal(price))
    )


def test_equity_and_per_symbol_unrealized_pnl_are_classified_at_tier_2_unbanded() -> None:
    """Tier-2 is recomputed on every read and never stored, so it cannot drift
    into future state — which is why it is classified but never healed
    (ADR-0034). It will also never be exact: the venue's mark is a robust median
    we do not replicate bit-for-bit.

    Un-banded here on purpose. The band is ADR-0040 §6's and lands with the
    alert slice; what this cycle owes it is a classified pair to apply a
    tolerance *to*, so a 0.018 gap that a band would certainly absorb is
    reported rather than silently dropped. Reporting it is the honest default:
    a divergence suppressed before any band exists is one no band was ever
    asked about.

    The ledger's own numbers are the fill's arithmetic, independent of the
    venue's: a 0.002 long entered at 64809 and marked at 65000 is worth
    ``0.002 × 191 = 0.382``, and equity is that on top of the 100000 cash line.
    The venue's snapshot agrees on size and on the implied cash line, so the
    Tier-1 halves are silent and the two Tier-2 figures are the whole finding —
    account grain first, as the cash line is.
    """
    store = SQLiteStore(":memory:")
    projection = _ledger(store, equity="100000")
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100000.400", ("BTC", "0.002", "0.400"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), portfolio=projection)

    divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == (
        Divergence(
            tier=DivergenceTier.TIER_2,
            field="equity",
            symbol=None,
            ledger=Decimal("100000.382"),
            venue=Decimal("100000.400"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_2,
            field="unrealized_pnl",
            symbol="BTC",
            ledger=Decimal("0.382"),
            venue=Decimal("0.400"),
        ),
    )


def test_a_tier_2_figure_the_ledger_cannot_yet_compute_is_not_reported_as_divergent() -> None:
    """The recovery window, and the one Tier-2 answer that must not be a
    divergence: no mark has arrived for a symbol the book holds, so uPnL and the
    equity Σ that contains it read ``None`` — uncomputable, not wrong (ADR-0041
    §6).

    Reported as disagreements they would alert on *our* missing input while
    naming the venue's figures as the fault, and they would fire on every start
    before the first mark lands — the noisiest possible false positive, on the
    one cadence an operator has to keep trusting.

    Tier-1 is unaffected and that is the asymmetry worth pinning: cash and size
    never need a mark, so the same cycle still cross-checks the accumulated
    ledger in full. A missing valuation input narrows the check; it does not
    freeze it, which is what the failed *anchor* read does.
    """
    store = SQLiteStore(":memory:")
    projection = _ledger(store, equity="100000")
    _book_fill(projection, quantity="0.002", price="64809")
    venue = _held("100000.400", ("BTC", "0.002", "0.400"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), portfolio=projection)

    assert projection.account().equity is None  # no mark yet, so no Σ to compare
    assert asyncio.run(cycle.reconcile_account()) == ()
