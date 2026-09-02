"""``LedgerReconciliation`` — the account-grain cycle that classifies what
disagrees with the venue (issue #193) and heals the Tier-1 half (issue #178).

Exercised through its public verbs against a **live**-shaped ledger over an
in-memory store and a venue double answering recorded account snapshots. The
venue is doubled because it is a process boundary (ADR-0022) and it is the only
thing doubled here: the ledger, its store and its clock are the real ones, so a
case asserts what the cycle *concludes* about a book a fill actually moved — and,
now that it writes, what it leaves behind in the store a real fill wrote to.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal

from ledgers import book_fill
from venue_doubles import LIVE_ACCOUNT_ID, LiveVenueDouble, account_state

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Account,
    AccountSpec,
    MarkTick,
    Order,
    OrderFilled,
    PlaceOrder,
    Position,
    Side,
    Store,
    VenueAccountState,
    VenueOrderView,
    VenueReadFailure,
)
from tickwright.engine.checkpoint import Checkpointer
from tickwright.engine.ledger_reconcile import (
    Divergence,
    DivergenceField,
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


def _ledger(store: Store, *, equity: str) -> Checkpointer:
    """A live ledger already materialised, the state every cycle finds it in.

    Live is the only shape that has a cycle at all — paper has no second account
    to compare against — so the genesis is ``None`` (ingested, ADR-0042 §6) and
    the opening cash line is the venue's own, taken from a snapshot holding no
    position so that ``equity`` *is* the cash a case declares.

    A ``Checkpointer`` rather than the bare projection, because the cycle now
    *writes*: a heal is checkpointed on the same atomic path a fill is, so the
    thing the cycle is constructed with has to be the type that owns that write.
    Reach the read-model through ``.portfolio`` where a case asserts on it.
    """
    keeper = Checkpointer(
        spec=AccountSpec(account_id=LIVE_ACCOUNT_ID, genesis_collateral=None),
        store=store,
        clock=ManualClock(7),
    )
    keeper.recover()
    keeper.portfolio.materialise(account_state(equity))
    return keeper


def _book_fill(
    projection: PortfolioProjection,
    *,
    quantity: str,
    price: str,
    symbol: str = "BTC",
    strategy_id: str = "alpha",
    side: Side = Side.BUY,
    seq: int = 1,
) -> None:
    """Fold one filled order into the ledger — the only way a partition exists.

    A case that wants the cycle to look at a *position* books one rather than
    writing the row itself: what the venue is compared against has to be a book
    the fill path actually produced.

    ``seq`` distinguishes a second fill of the same symbol, which is what a case
    closing a partition needs: the saga's identifiers are per-order, so a
    sell reusing the buy's would be the same fill arriving twice rather than the
    trade that flattened it.
    """
    book_fill(
        projection,
        OrderFilled(
            ts_event=1_000 * seq,
            ts_init=1_000 * seq,
            cloid=f"0x{strategy_id}-{symbol}-{seq}",
            strategy_id=strategy_id,
            signal_id=f"{strategy_id}:{symbol}:{seq}",
            symbol=symbol,
            trade_id=f"{symbol}-{seq}",
            quantity=Decimal(quantity),
            price=Decimal(price),
            cum_qty=Decimal(quantity),
            fee=Decimal("0"),
        ),
        side=side,
    )


def test_a_cycle_whose_snapshot_matches_the_ledger_reports_no_divergence() -> None:
    """The agreeing pass, and the anchor it rests on: **one** account read is the
    whole cycle's venue cost (ADR-0034), so the read count is also the assertion
    that nothing polls per symbol.

    An empty tuple is a book that agreed, and it is deliberately not the same
    answer as the freeze below: an outage is never a flat book (ADR-0011 inv 1).
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    venue = _AccountVenue(account_state("100000"))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

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
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    held = projection.open_positions(strategy_id="alpha")
    cash = projection.account().cash
    venue = _AccountVenue(None, account_state("100000"))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    with capture_events() as logs:
        frozen = asyncio.run(cycle.reconcile_account())

    assert frozen is None
    assert [log["event"] for log in logs] == [NamedEvent.ACCOUNT_RECONCILE_FROZEN.value]
    assert projection.open_positions(strategy_id="alpha") == held
    assert projection.account().cash == cash

    assert asyncio.run(cycle.reconcile_account()) is not None  # the venue came back
    assert venue.account_reads == 2


def _recorded(logs: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
    """The one ``account.reconciled`` record in ``logs``, whatever else the pass
    emitted.

    Selected rather than indexed, because a healing cycle is no longer the only
    thing that speaks: a Tier-1 heal announces its ``position.*`` changes on the
    way past, exactly as the fill path does. A case asserting on the *pass's own
    summary* should say so rather than depend on it being the only record, which
    is a coupling to the state of the book the case happens to build.
    """
    (record,) = [log for log in logs if log["event"] == NamedEvent.ACCOUNT_RECONCILED.value]
    return record


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
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _book_fill(projection, quantity="1.5", price="3000", symbol="ETH")
    _book_fill(projection, quantity="100", price="0.4", symbol="DOGE")
    venue = _held(
        "100000",
        ("BTC", "0.003", "0"),  # a fill the ledger missed
        ("DOGE", "100", "0"),  # agrees
        ("SOL", "10", "0"),  # flow the engine never placed
    )  # ETH: held by the ledger, flat at the venue
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == (
        Divergence(
            tier=DivergenceTier.TIER_1,
            field=DivergenceField.SIGNED_SIZE,
            symbol="BTC",
            ledger=Decimal("0.002"),
            venue=Decimal("0.003"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_1,
            field=DivergenceField.SIGNED_SIZE,
            symbol="ETH",
            ledger=Decimal("1.5"),
            venue=Decimal("0"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_1,
            field=DivergenceField.SIGNED_SIZE,
            symbol="SOL",
            ledger=Decimal("0"),
            venue=Decimal("10"),
        ),
    )


def test_a_size_divergence_heals_through_a_fill_into_the_unattributed_partition() -> None:
    """The Tier-1 heal, and the two halves of it that are one behavior.

    **It heals through a synthetic fill**, on the same idempotent ``apply()``
    path a venue-pushed one takes (ADR-0034), never a write to ``signed_size``.
    That is what leaves a "why did it move" record and what keeps every derived
    number consistent with the fill that produced it — the healed partition has
    an entry price and a cost basis, so the equity it contributes to is
    computable rather than a size floating free of a price.

    **It lands in the reserved unattributed partition**, never a strategy's:
    the venue has no per-strategy truth, so attributing foreign flow to whoever
    owns the symbol would both corrupt that strategy's PnL and let its
    close-my-position logic act on exposure it never opened (ADR-0038). The
    account net is the only thing reconciled, and the residual is what makes
    ADR-0034's Σ-invariant hold by construction.

    The venue holds SOL the ledger has never seen — foreign flow, the case
    where the two halves are separable at all: a heal that reached for the
    symbol's owning strategy would have none to find.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    venue = _AccountVenue(_held("100000", ("SOL", "10", "0")))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    asyncio.run(cycle.reconcile_account())

    ledger = keeper.portfolio
    assert ledger.account_net() == {"SOL": Decimal("10")}
    healed = ledger.position("SOL", strategy_id=None)
    assert healed is not None
    assert healed.size == Decimal("10")
    assert healed.entry_price == Decimal("64809")  # the venue's own, off the snapshot
    assert ledger.open_positions(strategy_id="alpha") == ()


def test_a_heal_is_durable_and_comes_back_on_the_restart_a_fill_comes_back_on() -> None:
    """A heal is checkpointed on the same atomic path a fill is (ADR-0034), so
    the restart that recovers the fill recovers the heal beside it.

    The in-memory read model agreeing with the venue is only half the correction.
    A heal that moved the projection and not the store leaves a restart reading a
    book that never healed — flat where the venue holds ten SOL — and the next
    cycle re-deriving the same divergence forever, which is the one failure the
    Σ-invariant holding *in the healing process* cannot rule out.

    Read back off the store rather than off the projection that healed, and then
    through a **second** ledger recovered from it with no venue read and no
    materialisation behind it: what the next process starts from is the rows, so
    the rows are what the claim has to be about. The healed partition arrives
    with the price it was booked at, since a size recovered without its basis is
    the free-floating number the synthetic fill exists to avoid.

    The store's own contract is asserted at ``test_checkpoint``'s seam; what is
    new here is that the *cycle* reaches it.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    venue = _AccountVenue(_held("100000", ("SOL", "10", "0")))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    asyncio.run(cycle.reconcile_account())

    assert [
        (position.strategy_id, position.symbol, position.signed_size, position.entry_price)
        for position in store.all_positions()
    ] == [(None, "SOL", Decimal("10"), Decimal("64809"))]

    restarted = Checkpointer(
        spec=AccountSpec(account_id=LIVE_ACCOUNT_ID, genesis_collateral=None),
        store=store,
        clock=ManualClock(7),
    )
    restarted.recover()
    assert restarted.portfolio.account_net() == {"SOL": Decimal("10")}
    assert restarted.portfolio.account().cash == keeper.portfolio.account().cash


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
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    venue = _held("100300", ("BTC", "0.002", "500"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == (
        Divergence(
            tier=DivergenceTier.TIER_1,
            field=DivergenceField.CASH,
            symbol=None,
            ledger=Decimal("100000"),
            venue=Decimal("99800"),
        ),
    )


def test_a_cash_divergence_heals_the_line_to_the_one_the_venue_implies() -> None:
    """The Tier-1 cash heal: the line is corrected **to** venue truth, not
    nudged toward it, and the correction is durable on the same transaction the
    size heals ride (ADR-0034).

    A **target, not a delta**, which is the whole shape of the verb. The venue
    publishes no cash line, so what the ledger is corrected to is the one its
    snapshot implies — ``equity − Σ uPnL``, the same derivation the genesis was
    ingested through — and a target absorbs whatever else moved the line in the
    same fold rather than stacking on top of it.

    **Genesis does not move.** The opening declaration is written once and is
    the account's identity, never recomputed from the line that has accrued away
    from it (ADR-0042 §3); a heal that touched it would erase the distance the
    ledger has travelled and make every later cash divergence read against the
    wrong origin. ADR-0042 §4's four accruing inputs are untouched too — this is
    a correction, not a fifth input.

    The venue reports a cash line 500 above the ledger's on an account holding
    nothing, which is the shape an operator's mid-run deposit arrives in: a
    known-benign alert that still heals, because the venue is authoritative.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    cycle = LedgerReconciliation(exchange=_AccountVenue(_held("100500")), checkpointer=keeper)

    asyncio.run(cycle.reconcile_account())

    assert keeper.portfolio.account().cash == Decimal("100500")
    restored = store.load_account()
    assert restored is not None
    assert restored.cash == Decimal("100500")
    assert restored.genesis_collateral == Decimal("100000")


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
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100000.400", ("BTC", "0.002", "0.400"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == (
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.EQUITY,
            symbol=None,
            ledger=Decimal("100000.382"),
            venue=Decimal("100000.400"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.UNREALIZED_PNL,
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
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    venue = _held("100000.400", ("BTC", "0.002", "0.400"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    assert projection.account().equity is None  # no mark yet, so no Σ to compare
    assert asyncio.run(cycle.reconcile_account()) == ()


def test_a_symbol_the_ledger_holds_flat_is_a_size_finding_and_not_a_second_one() -> None:
    """A symbol the ledger carries no exposure in is *not held*, and the two
    checks have to agree about that or the same disagreement is reported twice.

    Being flat and being absent are one state at Tier-1 — ``_sizes`` ranges over
    the union of both symbol sets with an absent side reading flat — so a symbol
    traded back to flat is exactly as unheld as one never traded. Tier-2 owes
    that the same answer: the venue still reporting exposure there is a missed
    fill, already named as a ``signed_size`` divergence, and the uPnL gap beside
    it is that one disagreement restated in another unit. The valuation is not
    wrong; the book is. Handed both, #194's band would have no honest response
    to the second but to suppress it.

    The record is the other half. Held-ness read two ways inflates ``tier_2``,
    which in this slice is the classification's only reader — an operator
    counting two Tier-2 findings on a book with one problem.

    Reachable on the ordinary book rather than at an edge: a flat partition is
    what every closed position leaves behind, so any symbol this engine has ever
    traded and closed lands here the moment the venue disagrees about it.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _book_fill(projection, quantity="0.002", price="64809", side=Side.SELL, seq=2)
    _mark(projection, "BTC", "65009")
    venue = _held("100000.400", ("BTC", "0.002", "0.400"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    assert projection.account_net() == {"BTC": Decimal("0")}  # the record survives the close
    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == (
        Divergence(
            tier=DivergenceTier.TIER_1,
            field=DivergenceField.SIGNED_SIZE,
            symbol="BTC",
            ledger=Decimal("0"),
            venue=Decimal("0.002"),
        ),
        # Genuinely the account grain's own: a flat book's equity is its cash,
        # and the venue's carries the 0.400 the ledger does not know it holds.
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.EQUITY,
            symbol=None,
            ledger=Decimal("100000"),
            venue=Decimal("100000.400"),
        ),
    )
    record = _recorded(logs)
    assert (record["tier_1"], record["tier_2"]) == (1, 1)


def test_a_completed_cycle_records_what_it_found_at_each_tier() -> None:
    """The pass is named, and the name alone is not the finding.

    The classification this cycle performs reaches its heal and its alert in
    later slices; it reaches an **operator** only here, through the record. A
    bare ``account.reconciled`` reads identically over a book that agreed and a
    book disagreeing at both tiers, which is the same defect the order grain's
    freeze already answers by carrying its scope: without the field an operator
    watching the event "cannot tell a pass that stopped dead from a pass that
    reconciled everything except one order" (``Reconciler._freeze``). Its two
    sibling records, ``inflight.reconciled`` and ``ghost.reconciled``, both name
    their resolution for the same reason.

    The counts are per tier because the tier is the whole of what classification
    decides — a Tier-1 finding is an accumulated gap that will be healed and a
    Tier-2 one is a valuation that will only ever be alerted on, so an operator
    reading a pass needs the split rather than a total.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100000.900", ("BTC", "0.003", "0.400"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    tiers = [divergence.tier for divergence in divergences]
    assert tiers.count(DivergenceTier.TIER_1) == 2  # the cash line and the size
    assert tiers.count(DivergenceTier.TIER_2) == 2  # equity and the symbol's uPnL
    record = _recorded(logs)
    assert (record["tier_1"], record["tier_2"], record["unvalued"]) == (2, 2, 0)


def test_a_pass_that_could_not_value_the_book_is_not_recorded_as_one_that_agreed() -> None:
    """The uncomputable Tier-2 above is right to classify nothing and wrong to
    say nothing.

    Dropping the figure is the honest classification — a valuation waiting on a
    mark is unknown, not divergent (ADR-0041 §6) — but it leaves the *record*
    of that pass identical to the record of a book that was fully cross-checked
    and agreed. An operator would read a run whose Tier-2 half never ran once as
    a run whose Tier-2 half agreed every time, which is inferring agreement from
    absence on the one cadence ADR-0011 inv 1 exists to keep honest.

    Reachable well past the first mark, too: marks are in-memory and never
    persisted, and the feed subscription is its own config list — so a restart
    still holding a position in a symbol the feed no longer carries leaves that
    symbol permanently unvalued, with every pass reporting a clean book.

    ``unvalued`` is a count rather than a suppression: this slice classifies and
    the band that would act on it is #194's, so what the cycle owes is that the
    two passes are told apart.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    venue = _held("100000.400", ("BTC", "0.002", "0.400"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as unvalued_logs:
        assert asyncio.run(cycle.reconcile_account()) == ()

    # 0.002 long entered at 64809 and marked at 65009 is worth the venue's own
    # 0.400, so the second pass genuinely agrees where the first could not look.
    _mark(projection, "BTC", "65009")
    with capture_events() as agreed_logs:
        assert asyncio.run(cycle.reconcile_account()) == ()

    assert _recorded(unvalued_logs)["unvalued"] == 2  # the account's equity and BTC's uPnL
    assert _recorded(agreed_logs)["unvalued"] == 0
    assert unvalued_logs != agreed_logs


class _SealedStore(SQLiteStore):
    """A real store that refuses every write once ``seal()`` is called.

    Sealed rather than write-counting because an identical-value write is still
    a write: the claim below is that a pass with nothing to heal is a **read**,
    and a write that happened to persist the number already there would slip
    past any before/after value comparison while being exactly the regression
    the claim exists to prevent. The seal closes over the *whole* write surface,
    not ``checkpoint_ledger`` alone, because the cycle reaches a ``Store`` only
    through its collaborators, so any verb arriving here is one of them growing
    a writer.

    Armed after setup, since materialising the ledger is itself a durable write
    and the subject is what the *cycle* does, not what opening one costs.
    """

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self._sealed = False

    def seal(self) -> None:
        self._sealed = True

    def _refuse(self, verb: str) -> None:
        if self._sealed:
            raise AssertionError(f"a classifying cycle wrote to the store via {verb}")

    def checkpoint_ledger(
        self,
        *,
        account: Account,
        positions: Sequence[Position] = (),
        order: Order | None = None,
        funding_mark: tuple[str, int] | None = None,
        ts_ns: int,
    ) -> None:
        self._refuse("checkpoint_ledger")
        super().checkpoint_ledger(
            account=account,
            positions=positions,
            order=order,
            funding_mark=funding_mark,
            ts_ns=ts_ns,
        )

    def checkpoint(self, order: Order, *, ts_ns: int) -> None:
        self._refuse("checkpoint")
        super().checkpoint(order, ts_ns=ts_ns)

    def save_strategy_snapshot(self, strategy_id: str, data: bytes, *, ts_ns: int) -> None:
        self._refuse("save_strategy_snapshot")
        super().save_strategy_snapshot(strategy_id, data, ts_ns=ts_ns)

    def save_kill_switch(self, *, tripped: bool, reason: str | None, ts_ns: int) -> None:
        self._refuse("save_kill_switch")
        super().save_kill_switch(tripped=tripped, reason=reason, ts_ns=ts_ns)


def test_a_cycle_that_finds_only_tier_2_divergence_changes_no_stored_value() -> None:
    """Tier-2 is **alerted on and never healed** (ADR-0034), and the store is
    where that has to be asserted: the only durable difference between an alert
    and a heal is a write.

    The distinction is easy to lose now that the same cycle does both. A pass
    that classified a valuation gap and then wrote *anything* would be healing
    toward a number the venue computes from a mark we deliberately do not
    replicate bit-for-bit — persisting rounding noise into Tier-1 state, which
    is precisely the accumulation the two-tier split exists to keep it out of.

    So the venue agrees on every Tier-1 figure and disagrees on both Tier-2
    ones: same size, same implied cash, a different open PnL and therefore a
    different equity. The book is left untouched — the account row at the cash
    it opened at, and the one position row still the strategy's own — against a
    store that refuses a write outright rather than being compared afterwards.
    """
    store = _SealedStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    opened = store.load_account()
    stored = store.all_positions()
    assert opened is not None
    # Equity 100000.500 against an unrealized 0.500 implies the ledger's own
    # cash exactly, so nothing at Tier-1 disagrees while both Tier-2 figures do.
    venue = _held("100000.500", ("BTC", "0.002", "0.500"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)
    store.seal()

    divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert {(d.tier, d.field, d.symbol) for d in divergences} == {
        (DivergenceTier.TIER_2, DivergenceField.EQUITY, None),
        (DivergenceTier.TIER_2, DivergenceField.UNREALIZED_PNL, "BTC"),
    }

    restored = store.load_account()
    assert restored is not None
    assert restored.cash == opened.cash == Decimal("100000")
    assert restored.genesis_collateral == opened.genesis_collateral
    assert store.all_positions() == stored


def test_a_size_finding_the_cycle_cannot_price_heals_nothing_and_writes_nothing() -> None:
    """A heal the cycle cannot compute is not attempted at half strength: it
    emits no synthetic fill, and a pass whose every finding is unhealable writes
    nothing at all.

    The reachable shape of "a heal of zero is not a fact worth putting on the
    path". ``clearinghouseState`` omits the entry price of a position the venue
    does not carry, and ADR-0034's synthetic needs a price to book against — so
    the ledger's own ETH, flat at the venue, is a Tier-1 finding with no price
    behind it. Booking it at an invented one would put a real number on
    ``entry_price`` that no later cycle re-examines, where an unhealed symbol
    simply diverges again at the next deadline and stays visible.

    **Still reported**, which is the half that makes the other half safe: the
    cycle declines to correct the book, not to notice. Silence here would be a
    position we believe we hold and do not, disappearing from the findings
    because it could not be priced.

    Asserted against a store that refuses a write outright, on the same argument
    the Tier-2 case makes below: an empty heal that re-stamped the account row
    would be indistinguishable from a correction by anything but the number, and
    a before/after comparison could not tell the two apart.
    """
    store = _SealedStore(":memory:")
    keeper = _ledger(store, equity="100000")
    _book_fill(keeper.portfolio, quantity="1.5", price="3000", symbol="ETH")
    cycle = LedgerReconciliation(exchange=_AccountVenue(_held("100000")), checkpointer=keeper)
    store.seal()

    divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == (
        Divergence(
            tier=DivergenceTier.TIER_1,
            field=DivergenceField.SIGNED_SIZE,
            symbol="ETH",
            ledger=Decimal("1.5"),
            venue=Decimal("0"),
        ),
    )
    assert keeper.portfolio.position("ETH", strategy_id=None) is None
    assert keeper.portfolio.account_net() == {"ETH": Decimal("1.5")}


def test_a_failed_barrier_read_is_recorded_under_the_grains_own_freeze_name() -> None:
    """The barrier's account read is the *same grain's* freeze as the cadence's,
    and it costs strictly more: a frozen cycle loses one pass, while this one
    exhausts the startup budget and faults the process (``invariants.md`` inv 1).
    It was nonetheless the silent one — a bare early return, so an operator got
    ``engine.faulted`` naming nothing, while the cheaper freeze one step later
    was fully named.

    So it reports ``account.reconcile_frozen``, the name the cadence already
    uses, rather than a second one: both are the account anchor coming back
    empty, and an operator reading the trail should not have to learn two
    vocabularies for one failure to distinguish an outage at boot from an outage
    an hour in. ``False`` is what the barrier retries on, and the ledger stays
    unopened — a row created from a read that never answered is the flat book
    ADR-0011 inv 1 refuses.
    """
    store = SQLiteStore(":memory:")
    keeper = Checkpointer(
        spec=AccountSpec(account_id=LIVE_ACCOUNT_ID, genesis_collateral=None),
        store=store,
        clock=ManualClock(7),
    )
    keeper.recover()
    cycle = LedgerReconciliation(exchange=_AccountVenue(None), checkpointer=keeper)

    with capture_events() as logs:
        materialised = asyncio.run(cycle.materialise_account())

    assert materialised is False
    assert [log["event"] for log in logs] == [NamedEvent.ACCOUNT_RECONCILE_FROZEN.value]
    assert store.load_account() is None


def test_each_account_freeze_names_the_caller_whose_cost_it_carries() -> None:
    """One name, two costs — so the name carries a ``scope``.

    The two callers of the account anchor fail identically and are answered
    identically: nothing is inferred from the missing read. What differs is the
    price. The cadence's freeze loses one pass and the next deadline reads
    again; the barrier's exhausts the startup budget and faults the process
    (``invariants.md`` inv 1). Told apart only by "the surrounding trail", an
    operator has to reconstruct which one they are looking at from what else
    happened to be logged nearby.

    A field rather than a second event name, because the catalog is closed
    (ADR-0020/0045) and nothing *routes* on the difference — which is exactly
    the call ``reconcile.frozen`` already makes for its own two scopes.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    cycle = LedgerReconciliation(exchange=_AccountVenue(None), checkpointer=keeper)

    with capture_events() as cadence_logs:
        assert asyncio.run(cycle.reconcile_account()) is None

    unopened = Checkpointer(
        spec=AccountSpec(account_id=LIVE_ACCOUNT_ID, genesis_collateral=None),
        store=SQLiteStore(":memory:"),
        clock=ManualClock(7),
    )
    unopened.recover()
    barrier = LedgerReconciliation(exchange=_AccountVenue(None), checkpointer=unopened)

    with capture_events() as barrier_logs:
        assert asyncio.run(barrier.materialise_account()) is False

    assert cadence_logs[0]["scope"] == "cadence"
    assert barrier_logs[0]["scope"] == "barrier"
