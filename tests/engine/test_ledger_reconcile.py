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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from decimal import Decimal

from ledgers import book_fill
from venue_doubles import LIVE_ACCOUNT_ID, LiveVenueDouble, account_state

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    EMPTY_LEVERAGE_BOOK,
    Account,
    AccountModeVerdict,
    AccountSpec,
    FundingAccrual,
    InstrumentSpec,
    LeverageBook,
    LeverageSpec,
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

    ``mode`` is the venue's verdict on its own abstraction mode, fixed for the
    double's life where ``answers`` varies per cycle: a mode switch is a
    deliberate master-wallet action, so a case that wants one models it by
    handing the cycle a venue already switched. ``mode_reads`` is public because
    "was the venue asked at all" is the assertion for the pass that had no heal
    to make (ADR-0046 §4 buys its steady-state cost by not asking).
    """

    def __init__(
        self,
        *answers: VenueAccountState | None,
        mode: AccountModeVerdict = AccountModeVerdict.VERIFIED,
    ) -> None:
        super().__init__(state=answers[0])
        self._answers = list(answers)
        self._mode = mode
        self.mode_reads = 0

    async def fetch_account_state(self) -> VenueAccountState | None:
        self.account_reads += 1
        return self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]

    async def verify_account_mode(self) -> AccountModeVerdict:
        self.mode_reads += 1
        return self._mode

    async def place(self, order: PlaceOrder) -> None:
        raise AssertionError("the account cycle places nothing")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("the account cycle cancels nothing")

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        raise AssertionError("the account cycle is anchored on the account snapshot, not a cloid")


class _SlowAccountVenue(_AccountVenue):
    """An account read with a **round trip inside it** — the shape a real one has.

    ``_AccountVenue`` answers from memory, so its ``await`` never actually
    suspends and the cycle runs as though the snapshot and the ledger fold were
    one instant. A live read is a POST: the loop runs whatever else is ready
    while it is in flight, and the fill handler is one of those things. This
    double is the difference, and it is the difference alone — the body it
    returns was serialised *before* ``during`` ran, exactly as a venue's is.
    """

    def __init__(self, *answers: VenueAccountState | None, during: Callable[[], None]) -> None:
        super().__init__(*answers)
        self._during = during

    async def fetch_account_state(self) -> VenueAccountState | None:
        state = await super().fetch_account_state()
        await asyncio.sleep(0)  # the wire: the loop is free to run the fill handler
        self._during()
        await asyncio.sleep(0)
        return state


def _ledger(
    store: Store,
    *,
    equity: str,
    clock: ManualClock | None = None,
    leverage: LeverageBook = EMPTY_LEVERAGE_BOOK,
    specs: Mapping[str, InstrumentSpec] | None = None,
) -> Checkpointer:
    """A live ledger already materialised, the state every cycle finds it in.

    Live is the only shape that has a cycle at all — paper has no second account
    to compare against — so the genesis is ``None`` (ingested, ADR-0042 §6) and
    the opening cash line is the venue's own, taken from a snapshot holding no
    position so that ``equity`` *is* the cash a case declares.

    A ``Checkpointer`` rather than the bare projection, because the cycle now
    *writes*: a heal is checkpointed on the same atomic path a fill is, so the
    thing the cycle is constructed with has to be the type that owns that write.
    Reach the read-model through ``.portfolio`` where a case asserts on it.

    ``clock`` is the case's own only where it runs **two** cycles, since a heal's
    key is stamped with the cycle's ``ts_ns``: on a clock that never moves, the
    second pass mints the first pass's ``event_id`` and is deduped away as a
    redelivery. Held still by default, which is what a single-cycle case wants.

    ``leverage`` and ``specs`` are the margin model's two inputs, defaulted away
    because most cases here are about the Tier-1 comparison and value nothing.
    A case that asserts on a Tier-2 *number* passes both, since without them the
    figure it wants is legitimately ``None``.
    """
    keeper = Checkpointer(
        spec=AccountSpec(account_id=LIVE_ACCOUNT_ID, genesis_collateral=None),
        store=store,
        clock=ManualClock(7) if clock is None else clock,
        leverage=leverage,
        specs=specs,
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

    The partition roster is read off the **store** rather than a strategy's view,
    because "never a strategy's" is a claim about every partition and a view
    scoped to one owner cannot make it: an implementation that filed the residual
    under some other id would leave the owner asked about empty and pass. The
    roster names the one partition that exists, so there is nowhere for it to
    hide.
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
    assert [(p.strategy_id, p.symbol) for p in store.all_positions()] == [(None, "SOL")]


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


def test_a_cash_heal_is_refused_when_the_mode_the_venue_reports_has_changed() -> None:
    """The heal above is the one thing a mid-run mode switch must not be allowed
    to perform (ADR-0046 §4).

    Under a pooled abstraction mode the perps clearinghouse reports only the
    collateral posted into perps, so ``equity − Σ uPnL`` reads an order of
    magnitude low with nothing in the response saying so — and ADR-0034 heals
    Tier-1 *toward* the venue, so the cycle would write that smaller figure into
    ``cash`` and ADR-0043 would persist it. The boot gate cannot close this: it
    ran once, and the switch is a deliberate master-wallet action taken since.

    So the mode is re-read before the heal and the heal is **refused** on
    anything but a verified answer. What is asserted here is the refusal in the
    two places it has to hold — the read-model and the store — and that the pass
    said **why** it stopped: an operator watching a cash line that stops healing
    with no record would be reading a silence.

    A **freeze and not a fault**: the read succeeded, so the pass still returns
    its classification rather than the ``None`` a failed anchor gives, and the
    engine keeps trading on a local ledger that is still correct.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    venue = _AccountVenue(_held("100500"), mode=AccountModeVerdict.CHANGED)
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert keeper.portfolio.account().cash == Decimal("100000")
    restored = store.load_account()
    assert restored is not None
    assert restored.cash == Decimal("100000")
    (unverified,) = [
        log for log in logs if log["event"] == NamedEvent.ACCOUNT_MODE_UNVERIFIED.value
    ]
    assert unverified["reason"] == "changed"
    assert not [log for log in logs if log["event"] == NamedEvent.ACCOUNT_HEALED.value]


def test_a_cash_heal_absorbs_the_pnl_the_same_passs_size_heal_realized() -> None:
    """The cash correction is folded **after** the size heals, and the ordering
    is the whole subject: a heal that closes into a partition realizes PnL onto
    the very line the same pass is correcting.

    Two cycles, because a *closing* heal needs a partition to close into and the
    only thing that can open an unattributed one is an earlier heal. The first
    books ten SOL at the venue's entry price. The second finds four, sells six at
    a higher price, and realizes 6 × 191 = 1146 on the way past.

    Corrected first, that 1146 would land on top of the venue's figure and the
    ledger would close the pass 1146 above a line it had just been told was
    right — reported next cycle as a fresh divergence that is nothing but this
    heal's own arithmetic, healed again, forever. Corrected last, the assignment
    absorbs it: the pass ends at exactly what the venue implies, which is why the
    verb takes a target and not a delta.

    So the number asserted is the venue's own and not the sum of anything: it is
    the same figure under both orderings only if the size heal realizes nothing,
    which is the case this one is built to avoid.
    """
    store = SQLiteStore(":memory:")
    clock = ManualClock(7)
    keeper = _ledger(store, equity="100000", clock=clock)
    opened = _held("100000", ("SOL", "10", "0"))
    closing = _held("101000", ("SOL", "4", "0"))
    closing = replace(
        closing, positions=(replace(closing.positions[0], entry_price=Decimal("65000")),)
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(opened, closing), checkpointer=keeper)

    asyncio.run(cycle.reconcile_account())  # opens the unattributed partition at 64809
    assert keeper.portfolio.account().cash == Decimal("100000")

    clock.advance_to(8)
    asyncio.run(cycle.reconcile_account())

    healed = keeper.portfolio.position("SOL", strategy_id=None)
    assert healed is not None
    assert healed.size == Decimal("4")
    assert healed.realized_pnl == Decimal("1146")
    assert keeper.portfolio.account().cash == Decimal("101000")


def test_each_heal_records_the_pair_it_moved_between_and_the_key_it_moved_under() -> None:
    """Every heal leaves a "why did it move" record (ADR-0034), one per heal and
    not one per pass.

    ``account.reconciled`` already counts what the cycle *found*, and a count is
    the wrong artifact for a ledger that has just been moved: an operator asking
    why a cash line jumped 500 overnight needs the figure it came from, the
    figure it went to, and the symbol — a tally of "one Tier-1 finding" answers
    none of those. So both sides ride the record, as they ride ``Divergence``,
    and for the same reason: a delta alone cannot tell a missed fill from a
    duplicated one.

    The **key** is on it because that is what makes the record auditable past
    this process. It is the ``event_id`` the synthetic was actually applied
    under, so an operator reading a healed partition in the store can join it to
    the pass that booked it, and a redelivery of the same heal is identifiable
    as the one that was deduped rather than a second correction.

    Both halves of one pass speak, because both moved the book: the size heal
    names its symbol and the cash correction carries none — the account has one
    collateral pool (ADR-0041 §2). One stamp is behind both keys, which is the
    cycle's own, so the pair reads as one retryable unit.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    venue = _AccountVenue(_held("100500", ("SOL", "10", "0")))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    with capture_events() as logs:
        asyncio.run(cycle.reconcile_account())

    assert [
        (log["field"], log["symbol"], log["ledger"], log["venue"], log["event_id"])
        for log in logs
        if log["event"] == NamedEvent.ACCOUNT_HEALED.value
    ] == [
        ("signed_size", "SOL", "0", "10", "reconcile:SOL:7"),
        ("cash", None, "100000", "100500", "reconcile:cash:7"),
    ]


def test_a_heal_leaves_the_funding_mark_where_it_found_it_and_a_correction_re_enters() -> None:
    """A funding correction is **not** a column the heal may write: it re-enters
    as a keyed ``FundingAccrual``, whose write is the only one allowed to
    advance the watermark it is guarded by (ADR-0043 §5.2/§9).

    The heal's write is the one that could break that rule, because it moves the
    same two things an accrual does — a partition's line and the account's cash —
    on a transaction the mark does not ride. Given a funding leg, it would move
    the funding line while the mark stayed put, and the next boundary the venue
    reports would then be *admitted* by a mark that never saw the correction and
    applied on top of it. That is §5.2's double-count reached through the one
    door the watermark does not cover, and the ledger has no later pass that
    finds it: funding is cumulative and no cycle re-derives it.

    So the claim is a pair. The heal books ten SOL the engine never placed and
    leaves the symbol's mark exactly as it found it — absent, the "never accrued"
    state that still admits any boundary — with the healed partition's funding
    line at zero. The correction that follows goes through the accrual path, and
    *there* the mark advances, in the same write as the line it guards.

    It accrues onto the **healed** partition, which is the case with no other
    owner: foreign flow is real exposure the venue charges funding on, and the
    unattributed partition is the only thing holding it (ADR-0038).
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    cycle = LedgerReconciliation(
        exchange=_AccountVenue(_held("100000", ("SOL", "10", "0"))), checkpointer=keeper
    )

    asyncio.run(cycle.reconcile_account())

    healed = keeper.portfolio.position("SOL", strategy_id=None)
    assert healed is not None
    assert healed.size == Decimal("10")
    assert healed.funding == Decimal("0")
    assert store.funding_mark("SOL") is None  # the heal advanced nothing
    assert keeper.portfolio.account().cash == Decimal("100000")

    keeper.checkpoint_funding(
        FundingAccrual(
            ts_event=3_600,
            ts_init=3_600,
            account_id=LIVE_ACCOUNT_ID,
            symbol="SOL",
            boundary_ts_ns=3_600,
            amount=Decimal("-12"),
        )
    )

    accrued = keeper.portfolio.position("SOL", strategy_id=None)
    assert accrued is not None
    assert accrued.funding == Decimal("-12")
    assert keeper.portfolio.account().cash == Decimal("99988")
    assert store.funding_mark("SOL") == 3_600


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


def test_a_retried_cycle_books_its_heal_once_and_announces_it_once() -> None:
    """A heal's key is the **cycle's** stamp, so one pass corrects a symbol once
    however many times that pass runs (``ReconciliationFill``).

    Content-keying is the alternative that looks more idempotent and is the
    trap: the same drift recurring a month later would collapse onto the first
    heal's id and never be booked at all. Stamping the cycle collapses the
    *retried* pass — the case idempotency is actually for — and still books a
    genuinely new divergence at the next deadline, which is a later stamp.

    The second pass here is handed a venue that has moved further, which is what
    makes the collapse visible at all: a re-read of the *same* snapshot finds a
    book that already agrees and would be a no-op whether the key deduped or
    not. Reported, then, but not applied — the finding rides the pass's own
    record where an operator can see it, and the next deadline heals it under a
    key of its own.

    **And nothing is announced, and nothing is written.** ``account.healed``
    answers "why did the ledger move", so a deduped synthetic must not emit one:
    it is the same rule that keeps a finding the cycle declined to price off the
    record, arriving through the one door the producer-side check does not cover
    — the synthetic here is well-formed, and it is the aggregate that refuses it.
    The store is the other half of that claim and the one a value comparison
    cannot make: a pass that re-stamped the account row with the figure already
    there would be indistinguishable from a correction by anything but the
    number, so the second pass runs against a store that refuses a write
    outright. Sealed after the first pass, since that one heals for real and its
    write is the state this case starts from.
    """
    store = _SealedStore(":memory:")
    keeper = _ledger(store, equity="100000")
    venue = _AccountVenue(_held("100000", ("SOL", "10", "0")), _held("100000", ("SOL", "12", "0")))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    asyncio.run(cycle.reconcile_account())
    store.seal()
    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    ledger = keeper.portfolio
    assert ledger.account_net() == {"SOL": Decimal("10")}  # the first pass's heal, not twice over
    healed = ledger.position("SOL", strategy_id=None)
    assert healed is not None
    assert healed.size == Decimal("10")
    assert [(d.field, d.ledger, d.venue) for d in divergences or ()] == [
        (DivergenceField.SIGNED_SIZE, Decimal("10"), Decimal("12"))
    ]
    assert [log for log in logs if log["event"] == NamedEvent.ACCOUNT_HEALED.value] == []


def test_a_heal_that_reduces_the_net_shorts_the_residual_rather_than_closing_a_strategy() -> None:
    """The residual is the **whole** correction and it lands in the unattributed
    partition — even when the ledger's own book is what is too large.

    The reducing direction is where "leave the strategies alone" stops being
    free. A venue holding less than the ledger has an obvious wrong answer
    sitting right there: sell the owning strategy down until the net agrees.
    That trades a size the strategy never traded, so it books realized PnL into
    that strategy's line and leaves its close-my-position logic acting on
    exposure it no longer has — the corruption ADR-0038 reserves the ``None``
    partition to prevent, arriving from the side where the arithmetic *would*
    have worked.

    So the heal shorts the residual into ``None`` instead, and the strategy's
    partition comes out bit-for-bit as the fill path left it: same size, same
    entry price, same realized line. Only the account net moves, which is the
    only grain the venue has an opinion about (ADR-0034).
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")
    held = ledger.position("BTC", strategy_id="alpha")
    venue = _AccountVenue(_held("100000", ("BTC", "0.0005", "0")))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    asyncio.run(cycle.reconcile_account())

    assert ledger.position("BTC", strategy_id="alpha") == held
    residual = ledger.position("BTC", strategy_id=None)
    assert residual is not None
    assert residual.size == Decimal("-0.0015")
    assert residual.entry_price == Decimal("64809")  # the venue's own, off the snapshot
    assert ledger.account_net() == {"BTC": Decimal("0.0005")}


def test_a_failed_read_never_un_heals_the_book_it_cannot_see() -> None:
    """A frozen cycle heals nothing and **removes** nothing, and the second half
    is the one that needs a heal in front of it to state at all.

    The book here holds ten SOL for one reason: a previous pass healed it there
    because the venue said so. Absence of the venue is not the venue saying the
    opposite (ADR-0011 inv 1) — so a read that fails must leave that partition
    exactly where the heal put it, rather than treating "not reported" as
    "reported flat" and shorting it back to zero. That inversion is available to
    anything that reconciles off the snapshot's symbol set without checking
    whether there *was* a snapshot, and it is the expensive direction: a spurious
    close writes a position the account never held, where a missed heal only
    waits a cadence interval.

    Asserted against a sealed store, on ``_size_heals``'s rule above: the freeze
    is an early return ahead of the checkpoint, so the claim is that the whole
    write surface stays untouched rather than that the numbers happen to match.
    The record says only that the pass froze — nothing was found, because
    nothing was compared, and nothing moved.
    """
    store = _SealedStore(":memory:")
    keeper = _ledger(store, equity="100000")
    ledger = keeper.portfolio
    venue = _AccountVenue(_held("100000", ("SOL", "10", "0")), None)
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    asyncio.run(cycle.reconcile_account())  # the pass that put the residual there
    healed = ledger.position("SOL", strategy_id=None)
    store.seal()

    with capture_events() as logs:
        frozen = asyncio.run(cycle.reconcile_account())

    assert frozen is None
    assert [log["event"] for log in logs] == [NamedEvent.ACCOUNT_RECONCILE_FROZEN.value]
    assert ledger.position("SOL", strategy_id=None) == healed
    assert ledger.account_net() == {"SOL": Decimal("10")}
    assert ledger.account().cash == Decimal("100000")
    assert {position.symbol for position in store.all_positions()} == {"SOL"}


def test_a_pass_compares_one_fold_so_its_own_cash_heal_is_never_a_tier_2_finding() -> None:
    """One cycle compares against **one** reading of the ledger, taken before
    anything it does can move the book.

    The two grains share inputs: ``equity`` is ``cash + Σ uPnL``, so the cash
    line Tier-1 heals is a term in the equity Tier-2 checks. Take the account
    view twice — once for the cash comparison and once for the equity one — and
    the heal in between makes the second reading a different book, so the cycle
    reports the venue as disagreeing by exactly the amount the cycle itself just
    moved. That is an alert about our own arithmetic, and it arrives through the
    one door ADR-0040 §6's suppression does not cover, because there is no
    Tier-1 *equity* finding for it to be suppressed against.

    The book here is built so the two answers differ. The ledger's mark is stale
    against the venue's, so the ledger's uPnL is 0.382 where the venue's is
    100.382 — a real Tier-2 disagreement — while the cash line is 100 too high
    by exactly the compensating amount. Equity therefore **agrees** on the fold
    the cycle found, and would disagree by 100 on the fold its own heal leaves
    behind. An equity finding on this pass is the bug, and its absence is the
    assertion.

    Not a rule the checks are asked to keep individually: ``reconcile_account``
    hoists the view, the net and the uPnL map above every heal, which is the
    same thing ``domain.valuation`` does within a single view — every field from
    one read, so two of them can never straddle a write.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")
    _mark(ledger, "BTC", "65000")
    venue = _AccountVenue(_held("100000.382", ("BTC", "0.002", "100.382")))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    divergences = asyncio.run(cycle.reconcile_account())

    assert [(d.field, d.ledger, d.venue) for d in divergences or ()] == [
        (DivergenceField.CASH, Decimal("100000"), Decimal("99900")),
        (DivergenceField.UNREALIZED_PNL, Decimal("0.382"), Decimal("100.382")),
    ]
    assert ledger.account().cash == Decimal("99900")  # the heal landed
    assert ledger.account().equity == Decimal("99900.382")  # and moved equity, after the fact


def test_a_healed_fill_and_the_venues_later_delivery_of_it_converge_on_one_application() -> None:
    """The venue's own fill arriving *after* the heal that stood in for it, and
    the account net ending where one application leaves it.

    The two cannot be deduped against each other, and the reason is structural
    rather than an omission: ``clearinghouseState`` reports **positions, never
    trades**, so the heal cannot know the ``trade_id`` the real fill will carry
    and its key cannot collide with ``{cloid}:fill:{trade_id}``. Keying them
    together would mean inventing an id for a trade the cycle never saw.

    So convergence is arithmetic rather than dedup, and it costs one cadence
    interval. The heal books the size into the unattributed partition; the
    venue's delivery books the same size into the strategy that placed it,
    leaving the ledger reading **double** until the next deadline; that pass
    finds the account net one size too large and heals the residual back out.
    The end state is the one that matters and all three parts of it are the
    claim: the account net is the venue's, the strategy owns the exposure it
    actually placed, and the unattributed partition is flat again — the heal
    withdrew, rather than leaving a permanent phantom beside the real fill.

    The over-read in the middle is asserted rather than glossed. It is the
    honest cost of the design and the thing an operator will see on a dashboard,
    so a change that quietly made it permanent should fail here.
    """
    store = SQLiteStore(":memory:")
    clock = ManualClock(7)
    keeper = _ledger(store, equity="100000", clock=clock)
    ledger = keeper.portfolio
    venue = _AccountVenue(_held("100000", ("BTC", "0.002", "0")))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    asyncio.run(cycle.reconcile_account())  # the heal stands in for a fill not yet delivered
    assert ledger.account_net() == {"BTC": Decimal("0.002")}

    # The venue delivers the trade the heal inferred, down the ordinary fill path.
    _book_fill(ledger, quantity="0.002", price="64809")
    assert ledger.account_net() == {"BTC": Decimal("0.004")}  # the over-read, for one interval

    clock.advance_to(8)  # the next deadline: a new pass, so a new heal key
    asyncio.run(cycle.reconcile_account())

    assert ledger.account_net() == {"BTC": Decimal("0.002")}
    held = ledger.position("BTC", strategy_id="alpha")
    assert held is not None
    assert held.size == Decimal("0.002")  # attribution is the strategy's, not the heal's
    residual = ledger.position("BTC", strategy_id=None)
    assert residual is not None
    assert residual.size == Decimal("0")  # the heal withdrew rather than lingering
    assert ledger.account().cash == Decimal("100000")  # closed at its own entry: nothing realized


def test_a_fill_landing_inside_the_account_read_is_healed_away_as_foreign_flow() -> None:
    """The snapshot is older than the fold it is compared against, and the cycle
    corrects the engine's own fill out of the account net.

    ``reconcile_account`` awaits the venue read and takes its three ledger folds
    *after* it returns. Everything after that await is synchronous, so no fill
    can interleave with the heal — but one can interleave with the **read**, and
    that is the gap. A live account read is a POST; a fill delivered while it is
    in flight is in the ledger and cannot be in the body already serialised. The
    cycle then reads the ledger as ahead of the venue and books the difference
    out, which is the exact inverse of the case it was built for.

    The reducing direction is what makes it reachable rather than theoretical:
    ``_size_heals`` needs the venue's entry price, and the venue supplies one
    precisely because it still carries the symbol. A fill that *opens* a
    partition is safe by accident — the snapshot omits a symbol it does not hold,
    so there is no price and no heal.

    What is asserted is the current behavior, not the desired one. Attribution
    survives (the correction lands in the unattributed partition, so the
    strategy's own book still reads the size it placed), and the damage is
    confined to the account grain, where the net now tracks a snapshot that
    predates the trade. The next pass reads a current snapshot and heals it back,
    so the ledger oscillates by the size of whatever landed in the window rather
    than drifting — but it churns the unattributed partition durably each way.
    A fix belongs at the comparison, not the heal: the snapshot carries its own
    venue timestamp, and a symbol whose ledger moved after it has not been
    compared against anything.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")

    def a_fill_lands() -> None:
        _book_fill(ledger, quantity="0.001", price="64810", seq=2)

    venue = _SlowAccountVenue(_held("100000", ("BTC", "0.002", "0")), during=a_fill_lands)
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    divergences = asyncio.run(cycle.reconcile_account())

    # The ledger is ahead because it saw the fill first, and the pass reports it
    # as the venue holding less — indistinguishable, here, from real foreign flow.
    assert [(d.field, d.symbol, d.ledger, d.venue) for d in divergences or ()] == [
        (DivergenceField.SIGNED_SIZE, "BTC", Decimal("0.003"), Decimal("0.002"))
    ]
    held = ledger.position("BTC", strategy_id="alpha")
    assert held is not None
    assert held.size == Decimal("0.003")  # attribution is untouched: both fills are the strategy's
    residual = ledger.position("BTC", strategy_id=None)
    assert residual is not None
    assert residual.size == Decimal("-0.001")  # the second fill, corrected back out
    assert ledger.account_net() == {"BTC": Decimal("0.002")}  # a net the venue no longer holds
    # The correction is durable, which is what makes this more than a bad read:
    # the row survives the restart the strategy's own partition would heal on.
    # Only the heal's row is here because ``_book_fill`` folds the projection
    # directly, the checkpoint being the cycle's rather than the fill path's.
    assert [(p.strategy_id, p.symbol, p.signed_size) for p in store.all_positions()] == [
        (None, "BTC", Decimal("-0.001"))
    ]


_BTC_LIQUIDATION = Decimal("52522.4977")
"""The venue's own ``clearinghouseState.liquidationPx``, as measured by #142 and
recorded in ADR-0040 §3 — the number live must read through rather than solve for."""

_BTC_SPEC = InstrumentSpec(
    symbol="BTC",
    sz_decimals=3,
    max_decimals=6,
    min_notional=Decimal("0"),
    margin_maint=Decimal("0.0125"),
)
"""The tier-0 maintenance rate #152 measured, so the computed branch has a real
answer to be *displaced by* rather than the ``None`` a specless projection gives."""

_BTC_CROSS_5X = LeverageBook(entries={"BTC": LeverageSpec(mode="cross", leverage=5)})
"""**Cross**, so the computed branch has real backing to solve against.

Isolated would not, on a *live* ledger: the locked bucket is the venue's to post
(ADR-0040 §3) and nothing here has posted one, so the formula would divide a
backing of nothing but unrealized PnL and answer with a price above the mark for
a long. Cross backs against account equity, which a materialised live ledger
holds — so what the venue's number displaces below is a credible price and not
an artefact."""


def _priced(state: VenueAccountState, price: Decimal | None) -> VenueAccountState:
    """The same snapshot with ``price`` on every position it carries.

    Built by replacement rather than by a second fixture so that the only thing
    differing from the recorded snapshot is the field under test.
    """
    return replace(
        state,
        positions=tuple(replace(p, liquidation_price=price) for p in state.positions),
    )


def test_a_cycle_caches_the_venues_liquidation_price_and_the_read_passes_it_through() -> None:
    """ADR-0040 §3's one read-through valuation, on the channel the ADR names for
    it: the reconcile pull, which ADR-0039 kept alive for exactly this.

    Live may not re-derive this number — the maintenance tier it needs is a
    fixed point, the tier depending on the position's value *at the price being
    solved for* — so the venue's own field is authoritative and our formula must
    step aside for it. That is a claim about which of two real numbers is
    reported, which is why the ledger here is given both margin-model inputs:
    the formula is answering (the assertion before the cycle proves it), and it
    is answering something else.

    The account is #142's own — 0.002 BTC long from 64809 against an equity of
    25.9264 — so the computed price lands a few units from the venue's and the
    two are genuinely rival answers to one question rather than a real number
    against a placeholder. The snapshot agrees with the ledger on every Tier-1
    line, so the cycle heals nothing and the only thing it leaves behind is the
    cache under test.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="25.9144", leverage=_BTC_CROSS_5X, specs={"BTC": _BTC_SPEC})
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "64815")
    venue = _AccountVenue(_priced(account_state("25.9264", "0.012"), _BTC_LIQUIDATION))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    before = projection.position("BTC", strategy_id="alpha")
    assert before is not None
    # The formula is live and answering — with a number that is not the venue's.
    assert before.liquidation_price is not None
    assert before.liquidation_price != _BTC_LIQUIDATION

    divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == ()
    after = projection.position("BTC", strategy_id="alpha")
    assert after is not None
    assert after.liquidation_price == _BTC_LIQUIDATION


def test_the_cached_liquidation_price_stays_frozen_until_the_next_cycle() -> None:
    """**Stale-frozen between reconciles** (ADR-0040 §3), which is the cost the
    read-through accepts: the number is only as fresh as the cadence.

    The freeze is asserted against a book that *moved* — a second fill at a
    different price shifts the entry, and with it every input the formula reads
    — so a read that answered with the formula again would visibly change here.
    It does not: the venue's number stands until a cycle replaces it, and the
    alternative (falling back to a computed price the moment anything moves)
    would report two different quantities under one name depending on how long
    ago the last read landed.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="25.9144", leverage=_BTC_CROSS_5X, specs={"BTC": _BTC_SPEC})
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "64815")
    venue = _AccountVenue(_priced(account_state("25.9264", "0.012"), _BTC_LIQUIDATION))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    asyncio.run(cycle.reconcile_account())

    _book_fill(projection, quantity="0.002", price="60000", seq=2)

    moved = projection.position("BTC", strategy_id="alpha")
    assert moved is not None
    # The book really did move under it — the entry is no longer the first fill's.
    assert moved.entry_price == Decimal("62404.5")
    assert moved.liquidation_price == _BTC_LIQUIDATION


def test_a_position_the_venue_prices_at_nothing_reads_none_rather_than_the_formula() -> None:
    """``liquidationPx`` absent is the venue *answering*, and the answer is that
    this position has no liquidation price (ADR-0040 §3).

    It is the majority case rather than a corner — 12 of 17 cross longs across
    the 22 mainnet accounts #142 sampled (ADR-0046 §6) — so the branch this
    pins is the one live spends most of its time on. The formula is answering
    with a real number here (the assertion before the cycle), and once the read
    lands it must stop: falling back to it would report a price for a position
    the venue says has none, which is the fabricated Tier-2 value ADR-0034's
    freeze-never-substitute rule exists to refuse.

    The same account and snapshot as the cache case above, so the only thing
    differing is the field under test.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="25.9144", leverage=_BTC_CROSS_5X, specs={"BTC": _BTC_SPEC})
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "64815")
    venue = _AccountVenue(_priced(account_state("25.9264", "0.012"), None))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    before = projection.position("BTC", strategy_id="alpha")
    assert before is not None
    assert before.liquidation_price is not None  # the formula is live and answering

    assert asyncio.run(cycle.reconcile_account()) == ()

    after = projection.position("BTC", strategy_id="alpha")
    assert after is not None
    assert after.liquidation_price is None


def test_a_position_opened_since_the_read_reads_none_rather_than_the_formula() -> None:
    """A symbol the snapshot never mentioned is *also* an absent venue price.

    Once a read has landed the projection is on the read-through branch for the
    whole book, not only for the symbols that cycle happened to carry — so an
    ETH position opened between deadlines reads ``None`` until the next pull
    prices it, rather than being quietly handed back to the formula. The
    alternative reports two different quantities under one name depending on
    whether a symbol made it into the last snapshot.

    ETH is given both margin-model inputs and a mark, so the formula had every
    input it needs and its silence here is the read-through's doing: the sibling
    Tier-2 figures assert exactly that, reading real numbers off the same view.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(
        store,
        equity="25.9144",
        leverage=LeverageBook(
            entries={
                "BTC": LeverageSpec(mode="cross", leverage=5),
                "ETH": LeverageSpec(mode="cross", leverage=5),
            }
        ),
        specs={"BTC": _BTC_SPEC, "ETH": replace(_BTC_SPEC, symbol="ETH")},
    )
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "64815")
    venue = _AccountVenue(_priced(account_state("25.9264", "0.012"), _BTC_LIQUIDATION))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    asyncio.run(cycle.reconcile_account())

    _book_fill(projection, quantity="1.5", price="3000", symbol="ETH", seq=2)
    _mark(projection, "ETH", "3000")

    eth = projection.position("ETH", strategy_id="alpha")
    assert eth is not None
    # Every input the formula reads is present — it is the read-through that silenced it.
    assert eth.maintenance_margin == Decimal("56.25")
    assert eth.margin_used is not None
    assert eth.liquidation_price is None
    btc = projection.position("BTC", strategy_id="alpha")
    assert btc is not None
    assert btc.liquidation_price == _BTC_LIQUIDATION  # the read did land


_BTC_ISOLATED_5X = LeverageBook(entries={"BTC": LeverageSpec(mode="isolated", leverage=5)})
"""**Isolated**, the mode whose ``margin_used`` is the locked bucket rather than
a division: ``isolated_collateral + unrealized_pnl`` (ADR-0040 §3, as corrected
by #142). It is the only mode for which the ingest below has anything to carry."""

_BTC_BUCKET = Decimal("25.898067")
"""The locked collateral #142 measured on a funded isolated position, recovered
by the adapter as ``marginUsed − unrealizedPnl`` (ADR-0043 §3)."""


def _isolated(state: VenueAccountState, collateral: Decimal | None) -> VenueAccountState:
    """The same snapshot with ``collateral`` on every position it carries.

    ``None`` is not an absent value here: it is the venue saying the position is
    **cross** and backed by the account pool, which is the claim the adapter's
    own ``_isolated_collateral`` refuses to guess at.
    """
    return replace(
        state,
        positions=tuple(replace(p, isolated_collateral=collateral) for p in state.positions),
    )


def test_a_cycle_ingests_the_venues_locked_collateral_onto_the_partition() -> None:
    """The bucket is the **venue's** to post on live, so it has to be read from
    the venue (ADR-0043 §3: "the reconcile still re-ingests it").

    Live never computes this number — ``_lock_isolated_collateral`` declines on
    the declared-versus-ingested predicate — so before the cycle the ledger's
    bucket is the ``0`` the dataclass opened at, and isolated ``margin_used``,
    which is ``isolated_collateral + unrealized_pnl``, reports the bare
    unrealized PnL wearing the bucket's name. That is not a small error: it is
    the position's mark-to-market where a reader expects its collateral.

    The snapshot agrees with the ledger on every Tier-1 line, so the cycle heals
    nothing and the ingest is the only thing it leaves behind.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="25.9144", leverage=_BTC_ISOLATED_5X, specs={"BTC": _BTC_SPEC})
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "64815")
    venue = _AccountVenue(_isolated(account_state("25.9264", "0.012"), _BTC_BUCKET))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    before = projection.position("BTC", strategy_id="alpha")
    assert before is not None
    # The uPnL alone, because nothing has posted a bucket: the degraded read.
    assert before.margin_used == Decimal("0.012")
    # And the denominator it is, which is where the damage is legible — a 5x
    # position reporting five figures of leverage, on a book that is fine.
    assert before.effective_leverage == Decimal("10802.5")

    divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == ()
    after = projection.position("BTC", strategy_id="alpha")
    assert after is not None
    assert after.margin_used == _BTC_BUCKET + Decimal("0.012")
    # The position is levered at what the operator set it to, give or take the
    # unrealized leg — which is the whole claim `effective_leverage` makes.
    assert after.effective_leverage is not None
    assert round(after.effective_leverage, 2) == Decimal("5.00")


def test_the_ingested_collateral_lands_in_the_cycles_own_transaction() -> None:
    """The bucket is **Tier-1 and durable** (ADR-0043 §3), which is the whole
    reason it cannot ride the memory-only cache the liquidation price does: the
    recovery window (§6) reads the ledger before the first reconcile of the next
    life, so a bucket that lived only in memory would read `0` there and report
    an isolated position's `margin_used` as its bare unrealized PnL again.

    It lands in the *cycle's* transaction rather than one of its own, because a
    pass's corrections are one answer to one snapshot: split across two writes,
    a crash between them leaves a durable state the venue never held.

    `_book_fill` folds the projection directly, so the store holds nothing until
    a checkpoint runs — which makes the row below the cycle's write and not the
    fill's.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="25.9144", leverage=_BTC_ISOLATED_5X, specs={"BTC": _BTC_SPEC})
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "64815")
    venue = _AccountVenue(_isolated(account_state("25.9264", "0.012"), _BTC_BUCKET))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    assert store.all_positions() == []

    asyncio.run(cycle.reconcile_account())

    assert [(p.symbol, p.isolated_collateral) for p in store.all_positions()] == [
        ("BTC", _BTC_BUCKET)
    ]
