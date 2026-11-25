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
from typing import Any, Final

import pytest
from ledgers import book_fill
from parking_clock import ParkingClock
from venue_doubles import (
    CROSSLESS_MAINTENANCE,
    LIVE_ACCOUNT_ID,
    RECORDED_ENTRY_PRICE,
    UNPOSTED_BUCKET,
    account_state,
    implied_free_margin,
    implied_notional,
    margined,
)

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    DEFAULT_LEVERAGE,
    EMPTY_LEVERAGE_BOOK,
    Account,
    AccountAnchor,
    AccountModeVerdict,
    AccountSpec,
    Denied,
    ExternalOrderRef,
    FundingAccrual,
    InstrumentSpec,
    LeverageBook,
    LeverageSpec,
    MarkTick,
    Order,
    OrderFilled,
    OrderType,
    PlaceSignal,
    Position,
    Side,
    Store,
    TimeInForce,
    VenueAccountState,
    VenueOpenOrder,
    VenuePositionState,
    VenueReadFailure,
)
from tickwright.engine.checkpoint import Checkpointer
from tickwright.engine.guard import PreTradeLimits, RealGuard, SymbolLimits
from tickwright.engine.ledger_reconcile import (
    Divergence,
    DivergenceField,
    DivergenceTier,
    LedgerReconciliation,
    ValuationBand,
)
from tickwright.engine.portfolio import PortfolioProjection
from tickwright.observability import NamedEvent
from tickwright.observability.testing import capture_events


class _AccountVenue:
    """A venue that answers the **account anchor** and holds nothing else.

    No base class, because this cycle is constructed against
    ``domain.AccountAnchor`` alone. It reads two of its members. The third,
    ``fetch_open_orders``, is operator cancel all's (ADR-0052). It used to extend
    ``LiveVenueDouble`` and stub ``place``/``cancel``/``fetch_order`` as
    assertion-raisers, and those raisers were the suite's way of saying that a
    cloid read or an order action reaching the seam is the specification being
    broken. The split says it one layer up instead: the cycle cannot ask for
    them, so a case cannot fail that way and mypy refuses the cycle that tries.
    A runtime assertion traded for a type is the whole point of the narrowing,
    and what is left here is only what a case actually varies.

    ``answers`` is consumed one per cycle and the last one repeats, so a case
    can put a **failed** read in front of a good one and assert the cadence
    recovers at its next deadline rather than staying frozen — a distinction a
    double answering one fixed value could not express.

    ``mode`` is the venue's verdict on its own abstraction mode, fixed for the
    double's life where ``answers`` varies per cycle: a mode switch is a
    deliberate master-wallet action, so a case that wants one models it by
    handing the cycle a venue already switched. Both counters are public because
    "was the venue asked at all" is the assertion for two cases — a restart that
    owes no account read, and the pass that had no heal to make (ADR-0046 §4
    buys its steady-state cost by not asking).
    """

    def __init__(
        self,
        *answers: VenueAccountState | None,
        mode: AccountModeVerdict = AccountModeVerdict.VERIFIED,
    ) -> None:
        self._answers = list(answers)
        self._mode = mode
        self.account_reads = 0
        self.mode_reads = 0

    async def fetch_open_orders(self) -> list[VenueOpenOrder | ExternalOrderRef] | VenueReadFailure:
        return []

    async def fetch_positions(self) -> dict[str, Decimal] | VenueReadFailure:
        return {}

    async def fetch_account_state(self) -> VenueAccountState | None:
        self.account_reads += 1
        return self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]

    async def verify_account_mode(self) -> AccountModeVerdict:
        self.mode_reads += 1
        return self._mode


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


class _SlowModeVenue(_AccountVenue):
    """A mode check with a round trip inside it (#700).

    The same difference ``_SlowAccountVenue`` makes, on the pass's other venue
    read. ``during`` runs once, inside the first mode check. The later checks
    answer at once, so a case can show the pass that heals after it.
    """

    def __init__(
        self,
        *answers: VenueAccountState | None,
        during: Callable[[], None],
        mode: AccountModeVerdict = AccountModeVerdict.VERIFIED,
    ) -> None:
        super().__init__(*answers, mode=mode)
        self._during = during

    async def verify_account_mode(self) -> AccountModeVerdict:
        verdict = await super().verify_account_mode()
        if self.mode_reads == 1:
            await asyncio.sleep(0)  # the wire: the loop is free to run the fill handler
            self._during()
            await asyncio.sleep(0)
        return verdict


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
    fee: str = "0",
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
            fee=Decimal(fee),
        ),
        side=side,
    )


def _posted(keeper: Checkpointer, *positions: tuple[str, str, str]) -> None:
    """An earlier cycle already posted the book's isolated buckets (#522).

    A live open leaves its bucket unposted, and the first cycle that carries the
    position posts it. That post is a Tier-1 write. So a case about a cycle that
    writes nothing has to start after it.
    """
    keeper.checkpoint_heal((), snapshot=_held("100000", *positions))


def _seen(cycle: LedgerReconciliation) -> None:
    """Run the pass before the one a case asserts on.

    A finding heals only when the previous pass found it too (#574). This pass
    finds the gaps and heals none of them, so the next pass can.
    """
    asyncio.run(cycle.reconcile_account())


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


def _held(
    equity: str,
    *positions: tuple[str, str, str],
    free_margin: str | None = None,
    entry: Mapping[str, str] | None = None,
    maintenance: str | None = None,
) -> VenueAccountState:
    """A venue snapshot holding an explicit ``(symbol, signed_size, uPnL)`` per entry.

    ``account_state`` answers the recorded BTC snapshot, which is the right
    shape for a materialisation and the wrong one for a *divergence*: a
    disagreement is a claim about which symbols each side holds and at what
    size, so the sizes have to be the case's own. Everything not compared stays
    the recorded snapshot's figures — what the cycle is handed is still a shape
    the venue could have returned.

    ``free_margin`` defaults to what this snapshot's *own* numbers imply, off
    the same ``implied_free_margin`` the recorded fixture derives its with — the
    roster differs between the two, the premise does not. A case that wants the
    disagreement passes its own.

    Each leg's ``notional`` is derived the same way, off ``implied_notional``,
    and the recorded 129.584 it replaces belongs to 0.002 BTC alone: a roster the
    case chose the sizes of has exposure the case chose too.

    ``entry`` is what that derivation reads the mark off, and a case whose book
    was not entered at the recorded 64809 has to say so: a leg's mark is the
    price its own entry and uPnL imply, so an ETH position priced off BTC's entry
    hands the cycle an exposure two orders of magnitude out and a divergence
    about nothing. It is the knob rather than the notional itself, so the two
    venue fields cannot be made to contradict each other.

    ``maintenance`` defaults to ``CROSSLESS_MAINTENANCE`` on that fixture's own
    premise — these rows are isolated 1x until a case ``_levered`` them, and the
    venue's field counts only cross ones — and a case running a cross book
    declares what its account would publish, there being no rate here to derive
    it from.
    """
    recorded_state = account_state(equity, "-0.034")
    recorded = recorded_state.positions[0]
    entered = entry if entry is not None else {}
    return VenueAccountState(
        equity=Decimal(equity),
        free_margin=implied_free_margin(
            equity, (unrealized for _, _, unrealized in positions), declared=free_margin
        ),
        cross_maintenance_margin=(
            CROSSLESS_MAINTENANCE if maintenance is None else Decimal(maintenance)
        ),
        as_of_ts_ns=recorded_state.as_of_ts_ns,
        cash_quantum=recorded_state.cash_quantum,
        positions=tuple(
            margined(
                replace(
                    recorded,
                    symbol=symbol,
                    signed_size=Decimal(size),
                    entry_price=(
                        entry_price := Decimal(entered.get(symbol, str(RECORDED_ENTRY_PRICE)))
                    ),
                    unrealized_pnl=Decimal(unrealized),
                    notional=implied_notional(Decimal(size), entry_price, Decimal(unrealized)),
                )
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
        entry={"DOGE": "0.4"},  # agrees on cost basis too, so net cash agrees (#582)
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
    _seen(cycle)

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
    _seen(cycle)

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

    The finding carries net cash (#582). Both sides hold 0.002 at 64809, a cost
    basis of 129.618, so the pair is 99870.382 against 99670.382. The gap is
    still 200.

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
            field=DivergenceField.NET_CASH,
            symbol=None,
            ledger=Decimal("99870.382"),
            venue=Decimal("99670.382"),
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
    _seen(cycle)

    asyncio.run(cycle.reconcile_account())

    assert keeper.portfolio.account().cash == Decimal("100500")
    restored = store.load_account()
    assert restored is not None
    assert restored.cash == Decimal("100500")
    assert restored.genesis_collateral == Decimal("100000")


def test_a_cash_gap_below_half_the_venue_unit_is_not_a_divergence() -> None:
    """ADR-0034 Tier 1 is exact once both sides are rounded to the venue's
    unit. A gap far below one unit is arithmetic, not a missed fill (#580).

    Two buys at different prices give an average entry that does not divide
    evenly. The close then realizes PnL with a remainder in the 25th decimal.
    By hand: 297.766845 + 3460.1 × 0.51 − (3456.7 × 0.47 + 3457.3 × 0.04)
    is 299.476845, which is the venue's figure.

    Two passes, because a finding heals only when the pass before found it too.
    The cycle runs unbanded, so the same remainder still shows as Tier-2 equity
    and free margin. Those only alert, and a real band covers them.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="297.766845")
    projection = keeper.portfolio
    _book_fill(projection, symbol="ETH", quantity="0.47", price="3456.7", seq=1)
    _book_fill(projection, symbol="ETH", quantity="0.04", price="3457.3", seq=2)
    _book_fill(projection, symbol="ETH", quantity="0.51", price="3460.1", seq=3, side=Side.SELL)
    ledger_cash = projection.account().cash
    assert ledger_cash != Decimal("299.476845")  # the remainder this case is about
    cycle = LedgerReconciliation(exchange=_AccountVenue(_held("299.476845")), checkpointer=keeper)
    _seen(cycle)

    divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [found for found in divergences if found.tier is DivergenceTier.TIER_1] == []
    assert projection.account().cash == ledger_cash


def test_a_cash_gap_of_one_venue_unit_still_heals() -> None:
    """Rounding to the venue's unit must not hide a real gap (#580). One unit
    is the smallest gap the venue can report, so it is a real divergence and
    heals like any other Tier-1 finding.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    cycle = LedgerReconciliation(
        exchange=_AccountVenue(_held("100000.000001")), checkpointer=keeper
    )
    _seen(cycle)

    asyncio.run(cycle.reconcile_account())

    assert keeper.portfolio.account().cash == Decimal("100000.000001")


def test_a_partial_close_the_venue_truncated_does_not_heal_cash_while_the_position_is_open() -> (
    None
):
    """Hyperliquid cuts a position's cost basis to 6 decimals on each partial
    close fill. Its cash line moves with that cut, and its net cash does not
    (#582). These are the fills from the testnet check on #582.

    Two buys give a cost basis of 242.219. Two sell fills close 0.001 of 0.003.
    The venue then holds a cost basis of 161.479332, measured. The exact one is
    161.4793333…, so the two cash lines differ by 1.33 micro-USDC. Net cash is
    1000 − 242.219 + 80734 × 0.00011 + 80730 × 0.00089 = 838.51144 on both sides.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="1000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.001", price="80739", seq=1)
    _book_fill(projection, quantity="0.002", price="80740", seq=2)
    _book_fill(projection, quantity="0.00011", price="80734", seq=3, side=Side.SELL)
    _book_fill(projection, quantity="0.00089", price="80730", seq=4, side=Side.SELL)
    ledger_cash = projection.account().cash
    venue = _held("999.990772", ("BTC", "0.002", "0"), entry={"BTC": "80739.666"})
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)
    _seen(cycle)

    divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [found for found in divergences if found.tier is DivergenceTier.TIER_1] == []
    assert projection.account().cash == ledger_cash


def test_closing_the_rest_of_a_truncated_position_to_flat_does_not_heal_cash() -> None:
    """At flat the venue's truncation is gone, and its cash line is exact again
    (#582, testnet step 6). So the ledger must not have healed while the
    position was open, or it would now be off by that heal and heal it back.

    The open book is the case above. A sell of the last 0.002 at 80750 adds
    161.5, so both sides end at 838.51144 + 161.5 = 1000.01144.
    """
    store = SQLiteStore(":memory:")
    clock = ManualClock(7)
    keeper = _ledger(store, equity="1000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.001", price="80739", seq=1)
    _book_fill(projection, quantity="0.002", price="80740", seq=2)
    _book_fill(projection, quantity="0.00011", price="80734", seq=3, side=Side.SELL)
    _book_fill(projection, quantity="0.00089", price="80730", seq=4, side=Side.SELL)
    venue = _AccountVenue(
        _held("999.990772", ("BTC", "0.002", "0"), entry={"BTC": "80739.666"}),
        _held("999.990772", ("BTC", "0.002", "0"), entry={"BTC": "80739.666"}),
        _held("1000.01144"),
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    passes = []
    for ts_ns in (8, 9):  # two passes while open, so a finding there could heal
        clock.advance_to(ts_ns)
        passes.append(asyncio.run(cycle.reconcile_account()))
    _book_fill(projection, quantity="0.002", price="80750", seq=5, side=Side.SELL)
    ledger_cash = projection.account().cash
    for ts_ns in (10, 11):  # two passes at flat, for the same reason
        clock.advance_to(ts_ns)
        passes.append(asyncio.run(cycle.reconcile_account()))

    for divergences in passes:
        assert divergences is not None
        assert [found for found in divergences if found.tier is DivergenceTier.TIER_1] == []
    assert projection.account().cash == ledger_cash
    assert ledger_cash.quantize(Decimal("0.000001")) == Decimal("1000.01144")


def test_a_net_cash_gap_of_one_unit_heals_while_a_truncated_position_is_open() -> None:
    """Comparing net cash must not hide a real gap (#582). The book is the
    truncated one above, and the venue holds one more micro-USDC.

    The heal sets net cash to the venue's 838.511441, then adds the ledger's
    own cost basis of 161.4793333…. So cash lands on 999.9907743…. Copying the
    venue's cash line, 999.990773, would bring in its truncation. The next pass
    would then find the same gap again.
    """
    store = SQLiteStore(":memory:")
    clock = ManualClock(7)
    keeper = _ledger(store, equity="1000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.001", price="80739", seq=1)
    _book_fill(projection, quantity="0.002", price="80740", seq=2)
    _book_fill(projection, quantity="0.00011", price="80734", seq=3, side=Side.SELL)
    _book_fill(projection, quantity="0.00089", price="80730", seq=4, side=Side.SELL)
    venue = _held("999.990773", ("BTC", "0.002", "0"), entry={"BTC": "80739.666"})
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)
    _seen(cycle)
    clock.advance_to(8)

    asyncio.run(cycle.reconcile_account())

    assert projection.account().cash.quantize(Decimal("0.000001")) == Decimal("999.990774")
    clock.advance_to(9)
    divergences = asyncio.run(cycle.reconcile_account())
    assert divergences is not None
    assert [found for found in divergences if found.tier is DivergenceTier.TIER_1] == []


def test_a_size_heal_and_a_cash_heal_in_one_pass_leave_net_cash_equal_to_the_venue() -> None:
    """The size heal books at the venue's entry, and the ledger's own entry
    stays on its own partition. So the two cost bases differ after the pass
    (#582). The cash heal must land net cash on the venue's anyway.

    The ledger holds 0.002 at 64809, a cost basis of 129.618. The venue holds
    0.003 at 65000, a cost basis of 195, and its net cash is 100010 − 195 =
    99815. The heal buys 0.001 at 65000, so the ledger's cost basis becomes
    194.618 and its cash 99815 + 194.618 = 100009.618.
    """
    store = SQLiteStore(":memory:")
    clock = ManualClock(7)
    keeper = _ledger(store, equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    venue = _held("100010", ("BTC", "0.003", "0"), entry={"BTC": "65000"})
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)
    _seen(cycle)
    clock.advance_to(8)

    asyncio.run(cycle.reconcile_account())

    assert projection.account_net() == {"BTC": Decimal("0.003")}
    assert projection.account().cash == Decimal("100009.618")
    clock.advance_to(9)
    divergences = asyncio.run(cycle.reconcile_account())
    assert divergences is not None
    assert [found for found in divergences if found.tier is DivergenceTier.TIER_1] == []


def test_a_cash_heal_waits_while_a_size_finding_in_the_same_pass_does_not_heal() -> None:
    """The cash heal adds the ledger's cost basis after the size fills (#582).
    A size finding that does not heal leaves that cost basis at the old size.
    Healed then, cash would be wrong by the missed fill's cost basis, and the
    next pass would find a new gap.

    The book is the case above. The first pass sees only the cash gap of 10.
    On the second the venue also holds 0.001 more BTC. That size finding is
    new, so it does not heal, and the cash heal must wait for it.
    """
    store = SQLiteStore(":memory:")
    clock = ManualClock(7)
    keeper = _ledger(store, equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    venue = _AccountVenue(
        _held("100010", ("BTC", "0.002", "0")),
        _held("100010", ("BTC", "0.003", "0"), entry={"BTC": "65000"}),
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)
    clock.advance_to(8)

    asyncio.run(cycle.reconcile_account())

    assert projection.account().cash == Decimal("100000")
    clock.advance_to(9)  # the size finding is confirmed now, so both heal
    asyncio.run(cycle.reconcile_account())
    assert projection.account_net() == {"BTC": Decimal("0.003")}
    assert projection.account().cash == Decimal("100009.618")
    clock.advance_to(10)
    divergences = asyncio.run(cycle.reconcile_account())
    assert divergences is not None
    assert [found for found in divergences if found.tier is DivergenceTier.TIER_1] == []


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
    _seen(cycle)

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


def test_a_mode_the_venue_cannot_report_refuses_the_heal_the_same_way_a_changed_one_does() -> None:
    """A read that fails is not evidence the mode is unchanged, so it takes the
    branch above rather than the healthy one (ADR-0046 §4).

    This is §3's boot rule in flight: never assume standard on error. The
    failure modes an unreadable answer covers — a timeout, an ``err`` envelope,
    a payload carrying no mode at all — are exactly the ones a switched account
    would also be reachable through, and the engine has no way to tell them
    apart from the outside. Guessing costs an operator the same wrong ``cash``
    line a genuine switch would.

    What differs from a changed mode is the **record**, and that is why the
    verdict carries three values where the control flow needs two: an operator
    told only "unverified" cannot tell an account somebody re-pooled from a
    venue that stopped answering, and those two want different actions.

    The mode is read **once**. There is no in-cycle retry because the cadence is
    the retry — the next deadline re-reads, and a heal deferred by one interval
    on an account whose ledger is still correct costs nothing, where a retry
    loop would spend a weight-20 request per attempt on the path that already
    only runs when something diverged.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    venue = _AccountVenue(_held("100500"), mode=AccountModeVerdict.UNREADABLE)
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)

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
    assert unverified["reason"] == "unreadable"
    assert venue.mode_reads == 1


def test_a_pass_with_no_cash_heal_to_make_never_asks_the_venue_for_its_mode() -> None:
    """The guard sits on the cash heal, not on the cycle, and that placement is
    what makes it affordable (ADR-0046 §4).

    ``userAbstraction`` is weight 20 against the anchor read's 2, so a per-cycle
    re-read would spend ten times as much on the watchman as on the thing
    watched, on every deadline forever, to catch an event that takes a
    deliberate master-wallet action. Asked only where the damage happens, it
    costs nothing in steady state — and a mode switch that produces no cash
    divergence is empty as a concern, since the switch *is* a re-pooling of
    balances and would show up as one.

    A **healing** pass is what proves the placement rather than an idle one: the
    cycle here writes a synthetic fill for foreign SOL flow and still asks the
    venue nothing, so what is pinned is that the gate hangs off the cash
    correction specifically and not off "the pass changed something".
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    venue = _AccountVenue(_held("100000", ("SOL", "10", "0")))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)

    with capture_events() as logs:
        asyncio.run(cycle.reconcile_account())

    assert venue.mode_reads == 0
    assert venue.account_reads == 2
    assert keeper.portfolio.account_net() == {"SOL": Decimal("10")}
    assert not [log for log in logs if log["event"] == NamedEvent.ACCOUNT_MODE_UNVERIFIED.value]


def test_a_cash_heal_absorbs_the_pnl_the_same_passs_size_heal_realized() -> None:
    """The cash correction is folded **after** the size heals, and the ordering
    is the whole subject: a heal that closes into a partition realizes PnL onto
    the very line the same pass is correcting.

    Two heals, because a *closing* heal needs a partition to close into and the
    only thing that can open an unattributed one is an earlier heal. Each heal
    runs on the second pass that sees its gap (#574). The first
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
    cycle = LedgerReconciliation(
        exchange=_AccountVenue(opened, opened, closing), checkpointer=keeper
    )
    _seen(cycle)

    asyncio.run(cycle.reconcile_account())  # opens the unattributed partition at 64809
    assert keeper.portfolio.account().cash == Decimal("100000")

    clock.advance_to(8)
    _seen(cycle)
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

    The cash pair is net cash (#582). SOL has a size finding, so both sides take
    the venue's cost basis of 10 × 150 = 1500: 98500 against 99000.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    venue = _AccountVenue(_held("100500", ("SOL", "10", "0"), entry={"SOL": "150"}))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)

    with capture_events() as logs:
        asyncio.run(cycle.reconcile_account())

    assert [
        (log["field"], log["symbol"], log["ledger"], log["venue"], log["event_id"])
        for log in logs
        if log["event"] == NamedEvent.ACCOUNT_HEALED.value
    ] == [
        ("signed_size", "SOL", "0", "10", "reconcile:SOL:7"),
        ("net_cash", None, "98500", "99000", "reconcile:cash:7"),
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
    _seen(cycle)

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


_MARK_TS_NS: Final = 2_000
"""When every ``_mark`` below is stamped, named because the staleness cases
measure a clock **against** it rather than merely feeding it in."""

_NS_PER_SECOND: Final = 1_000_000_000


def _mark(
    projection: PortfolioProjection, symbol: str, price: str, *, ts_ns: int = _MARK_TS_NS
) -> None:
    """Feed the projection the Tier-2 valuation input (ADR-0039).

    A Tier-2 case has to go through this rather than assert on a figure it
    passed in: uPnL and equity are *recomputed on every read* from the cached
    mark, so a case that never observed one is asserting on the ``None`` the
    absence produces, not on a valuation.

    ``ts_ns`` is the case's own only where it needs two marks of **different**
    ages in one book — a staleness rule that ranges over symbols is only tested
    by a book where the range matters.
    """
    projection.observe_mark(
        MarkTick(ts_event=ts_ns, ts_init=ts_ns, symbol=symbol, price=Decimal(price))
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
    ``0.002 × 191 = 0.382`` on a notional of 130.000, and equity is that uPnL on
    top of the 100000 cash line. The venue's snapshot agrees on size and on the
    implied cash line, so the Tier-1 halves are silent and the Tier-2 figures are
    the whole finding — account grain first, as the cash line is.

    Four of them, off one 0.018 skew: the venue's uPnL puts its mark at 65009,
    which is a different equity, a different uPnL, a different exposure *and* a
    different posted margin — the run is at the default isolated 1x, where a
    position's margin is the bucket it locked marked to market and the bucket
    here is the ``0`` no reconcile has yet ingested, so the figure moves with the
    uPnL exactly. A book where only one of the four moved would be the odd one.
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
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.NOTIONAL,
            symbol="BTC",
            ledger=Decimal("130.000"),
            venue=Decimal("130.0180"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.MARGIN_USED,
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


def test_a_pass_compares_against_the_reading_it_healed_from() -> None:
    """One reading per side per pass, and this is the book that punishes a second
    one.

    The ledger is flat and the venue holds 10 SOL entered at 64809, so the pass
    finds one thing — a size gap — and heals it by booking that position into the
    unattributed partition. Nothing else disagrees: with the venue carrying no
    open PnL, the cash line the snapshot implies is the 100,000 the ledger
    already holds, and a flat book's equity is its cash.

    The heal changes that, immediately and by construction. The ledger now holds
    10 SOL against a mark of 65,000, so its equity is 101,910 — 1,910 above the
    snapshot this pass was comparing against, which is precisely the position the
    pass just booked, valued at a mark the venue's own figure predates. Read
    again after the write, the cycle would report that as the venue disagreeing
    with us, and this is the shape where nothing catches it: a Tier-2 finding is
    suppressed on a grain Tier-1 already explains, the gap is on the **account**
    grain, and this pass has no account-grain finding — no cash divergence, since
    the cash line was right. The alert would go out.

    So the assertion is a silence with the counterfactual measured beside it: the
    pass says one thing, and the post-heal read the pass declined to take is
    asserted directly, as the number that alert would have carried. Without that
    second assertion the silence is vacuous — it would hold on a cycle that never
    compared anything.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _mark(projection, "SOL", "65000")
    venue = _held("100000", ("SOL", "10", "0"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)
    _seen(cycle)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == (
        Divergence(
            tier=DivergenceTier.TIER_1,
            field=DivergenceField.SIGNED_SIZE,
            symbol="SOL",
            ledger=Decimal("0"),
            venue=Decimal("10"),
        ),
    )
    assert _alerts(logs) == []
    record = _recorded(logs)
    assert (record["tier_1"], record["tier_2"], record["suppressed"]) == (1, 0, 0)
    # The counterfactual, taken after the pass: what a second reading would have
    # found is a real 1,910 gap, and every digit of it is this pass's own heal.
    assert projection.account_net() == {"SOL": Decimal("10")}
    assert projection.account().equity == Decimal("101910")


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
    # Equity, free margin, and the symbol's uPnL, notional and posted margin. The
    # first two are the cash gap restated in the units they are computed in —
    # both carry the cash line as a term — which is why ADR-0040 §6's first
    # suppression exists. It suppresses the *alert*; the pass still found five,
    # and the record says what it found.
    assert tiers.count(DivergenceTier.TIER_2) == 5
    record = _recorded(logs)
    assert (record["tier_1"], record["tier_2"], record["unvalued"]) == (2, 5, 0)


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

    # The account's two figures — equity and the free margin computed from it —
    # and all three of BTC's, its uPnL, its exposure and the margin posted
    # against it: the count is per figure the pass could not compare, not per
    # symbol it could not value.
    assert _recorded(unvalued_logs)["unvalued"] == 5
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

    So the venue agrees on every Tier-1 figure and disagrees on the Tier-2 ones:
    same size, same implied cash, a different open PnL and therefore a different
    equity, exposure and posted margin. The book is left untouched — the account row at the cash
    it opened at, and the one position row still the strategy's own — against a
    store that refuses a write outright rather than being compared afterwards.
    """
    store = _SealedStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _posted(keeper, ("BTC", "0.002", "0"))
    _mark(projection, "BTC", "65000")
    opened = store.load_account()
    stored = store.all_positions()
    assert opened is not None
    # Equity 100000.500 against an unrealized 0.500 implies the ledger's own
    # cash exactly, so nothing at Tier-1 disagrees while the Tier-2 figures do.
    venue = _held("100000.500", ("BTC", "0.002", "0.500"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)
    store.seal()

    divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert {(d.tier, d.field, d.symbol) for d in divergences} == {
        (DivergenceTier.TIER_2, DivergenceField.EQUITY, None),
        (DivergenceTier.TIER_2, DivergenceField.UNREALIZED_PNL, "BTC"),
        (DivergenceTier.TIER_2, DivergenceField.NOTIONAL, "BTC"),
        (DivergenceTier.TIER_2, DivergenceField.MARGIN_USED, "BTC"),
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


def test_a_mode_frozen_pass_writes_nothing_and_leaves_the_book_readable() -> None:
    """The refused heal above, asserted where a heal is *durable*: a freeze that
    dropped the correction from the projection and still stamped the account row
    would persist the pooled figure the guard exists to keep off disk
    (ADR-0043 persists what the cycle books, ADR-0046 §4).

    Sealed rather than compared, on ``_SealedStore``'s own rule: the refusal is
    that the pass is a **read**, and a write re-persisting the number already
    there would pass any before/after comparison while being the regression the
    claim is about.

    And a freeze is **not a fault**, which is the second half here. Our fills
    are still our fills and every Tier-2 figure is computed from
    ``(position, mark)`` rather than from the venue, so what stopped is the
    cross-check and not the ledger: the book stays readable through ``Portfolio``
    at the grain a strategy asks it at — its own partition, the account net and
    the cash line — and the engine keeps trading on it.
    """
    store = _SealedStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _posted(keeper, ("BTC", "0.002", "0"))
    held = projection.open_positions(strategy_id="alpha")
    venue = _AccountVenue(_held("100500", ("BTC", "0.002", "0")), mode=AccountModeVerdict.CHANGED)
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)
    store.seal()

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    (unverified,) = [
        log for log in logs if log["event"] == NamedEvent.ACCOUNT_MODE_UNVERIFIED.value
    ]
    assert unverified["reason"] == "changed"
    assert projection.open_positions(strategy_id="alpha") == held
    assert projection.account_net() == {"BTC": Decimal("0.002")}
    assert projection.account().cash == Decimal("100000")


def test_a_frozen_cash_grain_leaves_the_same_passs_size_heal_booking() -> None:
    """The freeze is scoped to the **account grain**, and one pass finding both
    kinds of divergence is where that scope is visible (ADR-0046 §4).

    A pooled abstraction mode misreports the collateral pool — that is what
    ``accountValue`` sums over — and says nothing about which positions the
    account holds: ``clearinghouseState`` reports the same signed sizes either
    way. So the position grain has no reason to stop, and stopping it would cost
    the engine the one heal that keeps ADR-0034's Σ-invariant true while the
    cash line waits for an operator.

    Both halves ride one transaction (``checkpoint_heal``), which is why this is
    asserted on the store rather than the projection alone: dropping the cash
    correction must leave the size heal's write intact rather than take the
    whole transaction down with it. The venue holds SOL the ledger has never
    seen *and* implies a cash line 500 above the ledger's — foreign flow beside
    a divergent pool, which is exactly the shape a re-pooling would produce.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    venue = _AccountVenue(_held("100500", ("SOL", "10", "0")), mode=AccountModeVerdict.CHANGED)
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)

    with capture_events() as logs:
        asyncio.run(cycle.reconcile_account())

    assert keeper.portfolio.account_net() == {"SOL": Decimal("10")}
    assert [(p.strategy_id, p.symbol, p.signed_size) for p in store.all_positions()] == [
        (None, "SOL", Decimal("10"))
    ]
    restored = store.load_account()
    assert restored is not None
    assert restored.cash == keeper.portfolio.account().cash == Decimal("100000")
    healed = [log for log in logs if log["event"] == NamedEvent.ACCOUNT_HEALED.value]
    assert [log["field"] for log in healed] == [DivergenceField.SIGNED_SIZE.value]


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
    healing = _held("100000", ("SOL", "10", "0"))
    venue = _AccountVenue(healing, healing, _held("100000", ("SOL", "12", "0")))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)

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
    _seen(cycle)

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

    Asserted against a sealed store, on the heal's rule above: the freeze
    is an early return ahead of the checkpoint, so the claim is that the whole
    write surface stays untouched rather than that the numbers happen to match.
    The record says only that the pass froze — nothing was found, because
    nothing was compared, and nothing moved.
    """
    store = _SealedStore(":memory:")
    keeper = _ledger(store, equity="100000")
    ledger = keeper.portfolio
    healing = _held("100000", ("SOL", "10", "0"))
    venue = _AccountVenue(healing, healing, None)
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)

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
    _seen(cycle)

    divergences = asyncio.run(cycle.reconcile_account())

    assert [(d.field, d.ledger, d.venue) for d in divergences or ()] == [
        # Net cash (#582): both cash lines less the cost basis of 129.618.
        (DivergenceField.NET_CASH, Decimal("99870.382"), Decimal("99770.382")),
        # The cash gap restated: free margin is ``cash − Σ buckets``, so the 100
        # the cash line is out by lands here too. Equity is the one that would
        # have to be re-derived to disagree, and it is the assertion.
        (DivergenceField.FREE_MARGIN, Decimal("100000"), Decimal("99900")),
        (DivergenceField.UNREALIZED_PNL, Decimal("0.382"), Decimal("100.382")),
        # The same stale mark, in exposure: the venue's uPnL puts its mark at
        # 115000 against the ledger's 65000.
        (DivergenceField.NOTIONAL, Decimal("130.000"), Decimal("230.0000")),
        # And in posted margin, which at the run's default isolated 1x is the
        # locked bucket marked to market — no bucket ingested yet, so it is the
        # uPnL line over again.
        (DivergenceField.MARGIN_USED, Decimal("0.382"), Decimal("100.382")),
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
    _seen(cycle)

    asyncio.run(cycle.reconcile_account())  # the heal stands in for a fill not yet delivered
    assert ledger.account_net() == {"BTC": Decimal("0.002")}

    # The venue delivers the trade the heal inferred, down the ordinary fill path.
    _book_fill(ledger, quantity="0.002", price="64809")
    assert ledger.account_net() == {"BTC": Decimal("0.004")}  # the over-read, for two passes

    clock.advance_to(8)  # the next deadline: a new pass, so a new heal key
    _seen(cycle)
    asyncio.run(cycle.reconcile_account())

    assert ledger.account_net() == {"BTC": Decimal("0.002")}
    held = ledger.position("BTC", strategy_id="alpha")
    assert held is not None
    assert held.size == Decimal("0.002")  # attribution is the strategy's, not the heal's
    residual = ledger.position("BTC", strategy_id=None)
    assert residual is not None
    assert residual.size == Decimal("0")  # the heal withdrew rather than lingering
    assert ledger.account().cash == Decimal("100000")  # closed at its own entry: nothing realized


def test_a_fill_landing_inside_the_account_read_is_reported_but_not_healed() -> None:
    """A symbol whose ledger moved while the read was in flight is not healed
    that pass (#284).

    A live account read is a POST. A fill delivered while it is in flight is in
    the ledger and cannot be in the body the venue already serialised. Compared
    as-is, the ledger reads ahead of the venue and the cycle books the engine's
    own fill out of the account net, durably, into the unattributed partition.
    The next pass heals it back, so the ledger oscillates instead of drifting,
    but it writes to the store each way.

    The reducing direction is what makes it reachable. The size heal needs the
    venue's entry price, and the venue supplies one precisely because it still
    carries the symbol. A fill that opens a partition is safe by accident, since
    the snapshot omits a symbol it does not hold.

    The finding is still reported. Declining to heal is the same answer the
    priceless-symbol arm gives, and a real divergence that happens to coincide
    with a fill must stay visible. The next deadline reads a current snapshot
    and compares the symbol for real.
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

    # Reported: the pass did see the ledger ahead of the snapshot it was handed.
    assert [(d.field, d.symbol, d.ledger, d.venue) for d in divergences or ()] == [
        (DivergenceField.SIGNED_SIZE, "BTC", Decimal("0.003"), Decimal("0.002"))
    ]
    # Not healed: both fills are the strategy's, and the account net keeps them.
    held = ledger.position("BTC", strategy_id="alpha")
    assert held is not None
    assert held.size == Decimal("0.003")
    assert ledger.position("BTC", strategy_id=None) is None
    assert ledger.account_net() == {"BTC": Decimal("0.003")}
    assert store.all_positions() == []


def test_a_fill_and_its_reverse_inside_the_read_still_defer_the_symbol() -> None:
    """A symbol any fill touched during the read is deferred, whether or not its
    net moved (#324).

    The rule #284 stated. Its detector compared the net fold on either side of
    the read, which is a proxy: a fill and its reverse inside one read window
    net to zero, so the symbol read as not moved and the pass healed a book
    that was never wrong. The venue here serialises after the first fill and
    before its reverse, so it carries 0.003 against a ledger that holds 0.002
    on both sides of the read.

    Reported and not healed. A heal would book the venue's transient 0.001
    into the unattributed partition, and the next pass would heal it back out:
    one wrong synthetic fill and two divergence records for a book that never
    drifted.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")

    def a_fill_and_its_reverse_land() -> None:
        # At the entry price, so the round trip realizes nothing and the cash
        # line stays out of the way: the case is about the size finding alone.
        _book_fill(ledger, quantity="0.001", price="64809", seq=2)
        _book_fill(ledger, quantity="0.001", price="64809", side=Side.SELL, seq=3)

    venue = _SlowAccountVenue(
        _held("100000", ("BTC", "0.003", "0")), during=a_fill_and_its_reverse_land
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert [(d.field, d.symbol, d.ledger, d.venue) for d in divergences or ()] == [
        (DivergenceField.SIGNED_SIZE, "BTC", Decimal("0.002"), Decimal("0.003"))
    ]
    assert ledger.position("BTC", strategy_id=None) is None
    assert ledger.account_net() == {"BTC": Decimal("0.002")}
    assert store.all_positions() == []
    # The pass says it held the finding back. Without the count, a deferred
    # pass and a healed one both read ``tier_1=1`` (#335).
    record = _recorded(logs)
    assert (record["tier_1"], record["deferred"], record["unpriced"]) == (1, 1, 0)


def test_a_symbol_that_did_not_move_during_the_read_still_heals_beside_one_that_did() -> None:
    """The deferral is per symbol, not per pass (#284).

    A fill on BTC during the read says nothing about SOL. The SOL finding was
    compared against a snapshot that is current for SOL, so it heals on this
    pass as it would on any other. Deferring the whole pass would let a busy
    symbol hold a real divergence on a quiet one open indefinitely.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")

    reads: list[None] = []

    def a_btc_fill_lands() -> None:
        # Only inside the second read. The first pass sees a quiet book and
        # finds SOL, so the second pass can heal it (#574).
        reads.append(None)
        if len(reads) == 2:
            _book_fill(ledger, quantity="0.001", price="64810", seq=2)

    venue = _SlowAccountVenue(
        _held("100000", ("BTC", "0.002", "0"), ("SOL", "10", "0")), during=a_btc_fill_lands
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)

    divergences = asyncio.run(cycle.reconcile_account())

    assert [(d.field, d.symbol, d.ledger, d.venue) for d in divergences or ()] == [
        (DivergenceField.SIGNED_SIZE, "BTC", Decimal("0.003"), Decimal("0.002")),
        (DivergenceField.SIGNED_SIZE, "SOL", Decimal("0"), Decimal("10")),
    ]
    assert ledger.account_net() == {"BTC": Decimal("0.003"), "SOL": Decimal("10")}
    # Only the unattributed rows. The first pass also posted alpha's BTC bucket.
    unattributed = [p for p in store.all_positions() if p.strategy_id is None]
    assert [(p.symbol, p.signed_size) for p in unattributed] == [("SOL", Decimal("10"))]


def test_a_symbol_the_ledger_never_held_opened_during_the_read_is_not_healed() -> None:
    """Movement is judged over the union of both folds, not over the symbols
    the ledger held before the read (#284).

    Before the read the ledger has no BTC record at all. The venue carries 0.002
    the engine never placed, and the engine's first BTC fill lands while the
    read is in flight. The snapshot carries an entry price, so this is the one
    open-from-absent shape that can reach the heal. A detector ranging over the
    pre-read symbols alone would miss it and book the venue's foreign 0.001
    into the unattributed partition against a ledger that has already moved.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    ledger = keeper.portfolio

    def a_first_btc_fill_lands() -> None:
        _book_fill(ledger, quantity="0.001", price="64810")

    venue = _SlowAccountVenue(_held("100000", ("BTC", "0.002", "0")), during=a_first_btc_fill_lands)
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    divergences = asyncio.run(cycle.reconcile_account())

    assert [(d.field, d.symbol, d.ledger, d.venue) for d in divergences or ()] == [
        (DivergenceField.SIGNED_SIZE, "BTC", Decimal("0.001"), Decimal("0.002"))
    ]
    assert ledger.account_net() == {"BTC": Decimal("0.001")}
    assert store.all_positions() == []


def test_a_cash_finding_on_a_pass_where_a_fill_landed_inside_the_read_is_not_healed() -> None:
    """The read-window deferral covers the cash line too (#330).

    A fill inside the read moves more than the size. It accrues its fee and any
    realized PnL to the cash line (ADR-0042 §4). The venue body was serialised
    before the fill, so its implied cash does not carry them. Compared as-is,
    the pass reads the engine's own fee as a gap and heals it away. The next
    pass reads the venue with the fee booked and heals it back.

    Reported and not healed, the answer the size arm gives. The fill lands at
    the entry price, so it moves cash by the fee alone: the expected line
    is the flat 100000 less 5, not a figure derived the way the ledger derives
    it.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    ledger = keeper.portfolio

    def a_fill_with_a_fee_lands() -> None:
        _book_fill(ledger, quantity="0.001", price="64810", fee="5")

    venue = _SlowAccountVenue(_held("100000"), during=a_fill_with_a_fee_lands)
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert [(d.field, d.symbol, d.ledger, d.venue) for d in divergences or ()] == [
        (DivergenceField.NET_CASH, None, Decimal("99995"), Decimal("100000")),
        (DivergenceField.SIGNED_SIZE, "BTC", Decimal("0.001"), Decimal("0")),
    ]
    assert ledger.account().cash == Decimal("99995")
    assert [log for log in logs if log["event"] == NamedEvent.ACCOUNT_HEALED.value] == []


def test_a_cash_finding_deferred_by_the_read_window_never_asks_the_venue_for_its_mode() -> None:
    """The mode gate hangs off the correction, not off the finding (#330).

    ADR-0046 §4 buys its steady-state cost by asking the venue only when a
    cash correction is about to be written. A finding the read window deferred
    writes nothing, so the pass has nothing to guard and the ``userAbstraction``
    read stays unspent. Pinned here because the deferral is a second way a cash
    finding ends the pass without a correction, and the gate could as easily
    have been wired to the finding.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    ledger = keeper.portfolio

    def a_fill_with_a_fee_lands() -> None:
        _book_fill(ledger, quantity="0.001", price="64810", fee="5")

    venue = _SlowAccountVenue(_held("100000"), during=a_fill_with_a_fee_lands)
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert [d.field for d in divergences or ()] == [
        DivergenceField.NET_CASH,
        DivergenceField.SIGNED_SIZE,
    ]
    assert venue.mode_reads == 0
    assert venue.account_reads == 1
    # Both findings were held back, and the record says so (#335).
    record = _recorded(logs)
    assert (record["tier_1"], record["deferred"], record["unpriced"]) == (2, 2, 0)


@pytest.mark.parametrize(
    ("fill", "cash", "after", "entry"),
    [
        pytest.param(
            {"symbol": "BTC", "side": Side.BUY, "quantity": "0.002", "price": "64809"},
            "99999.95",
            (("BTC", "0.004", "0"),),
            None,
            id="buy",
        ),
        pytest.param(
            {"symbol": "BTC", "side": Side.SELL, "quantity": "0.002", "price": "65809"},
            "100001.95",
            (),
            None,
            id="closing sell",
        ),
        pytest.param(
            {"symbol": "ETH", "side": Side.BUY, "quantity": "0.1", "price": "3000"},
            "99999.95",
            (("BTC", "0.002", "0"), ("ETH", "0.1", "0")),
            {"ETH": "3000"},
            id="buy of another symbol",
        ),
    ],
)
def test_a_fill_landing_inside_the_mode_check_defers_the_cash_heal(
    fill: Mapping[str, Any],
    cash: str,
    after: tuple[tuple[str, str, str], ...],
    entry: Mapping[str, str] | None,
) -> None:
    """The mode check is a venue read too, so a fill can land inside it (#700).

    The cash heal adds the ledger's cost basis to the venue's net cash. The
    fill moves that cost basis and the venue's figure predates the fill. Healed
    then, cash is wrong by the whole cost of the fill. So the heal waits.

    The venue's cash is 5 above the ledger's throughout. The fill pays a fee of
    0.05, and the closing sell realizes 2. After the fill the gap of 5 is still
    there. The next pass sees it again and the one after heals it.
    """
    clock = ManualClock(7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")
    before = _held("100005", ("BTC", "0.002", "0"))
    venue = _SlowModeVenue(
        before,
        before,
        _held(str(Decimal(cash) + 5), *after, entry=entry),
        during=lambda: _book_fill(ledger, seq=2, fee="0.05", **fill),
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)
    clock.advance_to(8)

    with capture_events() as logs:
        asyncio.run(cycle.reconcile_account())

    assert ledger.account().cash == Decimal(cash)
    assert [log for log in logs if log["event"] == NamedEvent.ACCOUNT_HEALED.value] == []
    record = _recorded(logs)
    assert (record["tier_1"], record["deferred"]) == (1, 1)
    clock.advance_to(9)
    asyncio.run(cycle.reconcile_account())
    assert ledger.account().cash == Decimal(cash)
    clock.advance_to(10)
    asyncio.run(cycle.reconcile_account())
    assert ledger.account().cash == Decimal(cash) + 5


def test_a_funding_payment_landing_inside_the_mode_check_defers_the_cash_heal() -> None:
    """A funding payment moves the cash line without a fill (#700).

    The venue's figure predates the payment. Healed toward it, the ledger loses
    the payment it just booked. So the heal waits, as it does for a fill.
    """
    clock = ManualClock(7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")

    def a_funding_payment_lands() -> None:
        keeper.checkpoint_funding(
            FundingAccrual(
                ts_event=3_600,
                ts_init=3_600,
                account_id=LIVE_ACCOUNT_ID,
                symbol="BTC",
                boundary_ts_ns=3_600,
                amount=Decimal("-0.5"),
            )
        )

    before = _held("100005", ("BTC", "0.002", "0"))
    venue = _SlowModeVenue(
        before,
        before,
        _held("100004.5", ("BTC", "0.002", "0")),
        during=a_funding_payment_lands,
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)
    clock.advance_to(8)

    with capture_events() as logs:
        asyncio.run(cycle.reconcile_account())

    assert ledger.account().cash == Decimal("99999.5")
    assert [log for log in logs if log["event"] == NamedEvent.ACCOUNT_HEALED.value] == []
    record = _recorded(logs)
    assert (record["tier_1"], record["deferred"]) == (1, 1)
    clock.advance_to(9)
    asyncio.run(cycle.reconcile_account())
    assert ledger.account().cash == Decimal("99999.5")
    clock.advance_to(10)
    asyncio.run(cycle.reconcile_account())
    assert ledger.account().cash == Decimal("100004.5")


@pytest.mark.parametrize("mode", list(AccountModeVerdict))
@pytest.mark.parametrize(
    ("side", "quantity", "size"),
    [
        pytest.param(Side.BUY, "0.002", "0.004", id="buy"),
        pytest.param(Side.SELL, "0.001", "0.001", id="sell"),
    ],
)
def test_a_venue_first_fill_landing_inside_the_mode_check_is_not_booked_twice(
    side: Side, quantity: str, size: str, mode: AccountModeVerdict
) -> None:
    """A size heal is stale too when its own fill lands in the mode check (#700).

    The venue booked the fill two passes ago, so the size gap is confirmed. The
    engine then books the fill while the mode is checked. The heal planned
    before it would book the same fill a second time.

    The mode verdict gates the cash heal alone. So the size heal must be held
    back on all three verdicts, and ``deferred`` counts both heals on each.
    """
    clock = ManualClock(7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")
    venue = _SlowModeVenue(
        _held("100005", ("BTC", size, "0")),
        during=lambda: _book_fill(ledger, quantity=quantity, price="64809", side=side, seq=2),
        mode=mode,
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)
    clock.advance_to(8)

    with capture_events() as logs:
        asyncio.run(cycle.reconcile_account())

    assert ledger.account_net() == {"BTC": Decimal(size)}
    assert ledger.position("BTC", strategy_id=None) is None
    assert ledger.account().cash == Decimal("100000")
    assert [log for log in logs if log["event"] == NamedEvent.ACCOUNT_HEALED.value] == []
    record = _recorded(logs)
    assert (record["tier_1"], record["deferred"]) == (2, 2)
    # Only the old cash gap is left. The size agrees without a heal.
    clock.advance_to(9)
    divergences = asyncio.run(cycle.reconcile_account())
    assert [
        (found.field, found.venue - found.ledger)
        for found in divergences or ()
        if found.tier is DivergenceTier.TIER_1
    ] == [(DivergenceField.NET_CASH, Decimal("5"))]


def test_a_size_heal_on_another_symbol_still_books_beside_a_fill_in_the_mode_check() -> None:
    """Only the symbols a fill touched lose their size heal (#700).

    The ETH position was opened by hand, and two passes have seen it. A BTC fill
    says nothing about it. Held back too, it would stay out of the ledger for
    two more passes. The cash heal still waits, so ``deferred`` counts one.
    """
    clock = ManualClock(7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")
    venue = _SlowModeVenue(
        _held("100005", ("BTC", "0.002", "0"), ("ETH", "0.1", "0"), entry={"ETH": "3000"}),
        during=lambda: _book_fill(ledger, quantity="0.002", price="64809", seq=2, fee="0.05"),
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)
    clock.advance_to(8)

    with capture_events() as logs:
        asyncio.run(cycle.reconcile_account())

    assert ledger.account_net() == {"BTC": Decimal("0.004"), "ETH": Decimal("0.1")}
    opened_by_hand = ledger.position("ETH", strategy_id=None)
    assert opened_by_hand is not None
    assert opened_by_hand.entry_price == Decimal("3000")
    assert ledger.position("BTC", strategy_id=None) is None
    assert ledger.account().cash == Decimal("99999.95")
    record = _recorded(logs)
    assert (record["tier_1"], record["deferred"]) == (2, 1)


@pytest.mark.parametrize(
    ("write", "cash"),
    [
        pytest.param("ETH fill", "99999.95", id="fill on another symbol"),
        pytest.param("BTC funding", "99999.5", id="funding on the same symbol"),
    ],
)
def test_a_size_heal_still_books_when_its_symbol_was_filled_only_before_the_mode_check(
    write: str, cash: str
) -> None:
    """Only a fill inside the mode check makes a size heal stale (#700).

    BTC has a fill from earlier in the run, and two passes have seen its size
    gap. What lands in the check is a fill on ETH, or a funding payment, which
    moves no size. The earlier BTC fill is already in the reading the heal was
    planned on. So the heal books, and ``deferred`` counts the cash heal alone.
    """
    clock = ManualClock(7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")

    def a_write_lands() -> None:
        if write == "ETH fill":
            _book_fill(ledger, symbol="ETH", quantity="0.1", price="3000", seq=2, fee="0.05")
            return
        keeper.checkpoint_funding(
            FundingAccrual(
                ts_event=3_600,
                ts_init=3_600,
                account_id=LIVE_ACCOUNT_ID,
                symbol="BTC",
                boundary_ts_ns=3_600,
                amount=Decimal("-0.5"),
            )
        )

    venue = _SlowModeVenue(_held("100005", ("BTC", "0.004", "0")), during=a_write_lands)
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    _seen(cycle)
    clock.advance_to(8)

    with capture_events() as logs:
        asyncio.run(cycle.reconcile_account())

    assert venue.mode_reads == 1
    assert ledger.account_net()["BTC"] == Decimal("0.004")
    healed = ledger.position("BTC", strategy_id=None)
    assert healed is not None
    assert healed.size == Decimal("0.002")
    assert ledger.account().cash == Decimal(cash)
    record = _recorded(logs)
    assert (record["tier_1"], record["deferred"]) == (2, 1)


def test_the_max_position_holds_after_a_venue_first_sell_lands_inside_the_mode_check() -> None:
    """A sell booked twice reads as a smaller position than the venue holds (#700).

    The venue holds 0.001 BTC after the sell. The cap is 0.002, so a buy of
    0.002 must be denied. Booked twice, the sell left the ledger flat, and the
    guard let the buy through.
    """
    clock = ManualClock(7)
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000", clock=clock)
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")
    venue = _SlowModeVenue(
        _held("100005", ("BTC", "0.001", "0")),
        during=lambda: _book_fill(ledger, quantity="0.001", price="64809", side=Side.SELL, seq=2),
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    guard = RealGuard(
        specs={"BTC": _BTC_SPEC},
        store=store,
        clock=clock,
        limits=PreTradeLimits(symbols={"BTC": SymbolLimits(max_position=Decimal("0.002"))}),
    )
    _seen(cycle)
    clock.advance_to(8)

    asyncio.run(cycle.reconcile_account())

    decision = guard.check(
        PlaceSignal(
            ts_event=8,
            ts_init=8,
            strategy_id="alpha",
            symbol="BTC",
            seq=3,
            side=Side.BUY,
            quantity=Decimal("0.002"),
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            price=Decimal("64809"),
        ),
        keeper.pre_trade_reading("BTC", Side.BUY, strategy_id="alpha"),
    )
    assert decision == Denied(reason="above max position 0.002")


def test_a_fill_the_venue_booked_first_is_not_healed_before_the_engine_books_it() -> None:
    """A size finding heals only when the previous pass found it too (#574).

    The venue books a fill before the engine learns of it. One pass reads the
    venue in that gap. A heal there would book the fill once, and the engine
    would book it again when it arrives. Here the engine books it before the
    next pass, so neither pass heals.
    """
    store = SQLiteStore(":memory:")
    clock = ManualClock(7)
    keeper = _ledger(store, equity="100000", clock=clock)
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")
    venue = _AccountVenue(_held("100000", ("BTC", "0.003", "0")))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    with capture_events() as first:
        asyncio.run(cycle.reconcile_account())
    _book_fill(ledger, quantity="0.001", price="64809", seq=2)
    clock.advance_to(8)
    with capture_events() as second:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == ()
    assert ledger.account_net() == {"BTC": Decimal("0.003")}
    assert ledger.position("BTC", strategy_id=None) is None
    healed = [log for log in [*first, *second] if log["event"] == NamedEvent.ACCOUNT_HEALED.value]
    assert healed == []
    record = _recorded(first)
    assert (record["tier_1"], record["unconfirmed"]) == (1, 1)


def test_a_funding_payment_the_venue_booked_first_is_not_healed_into_cash() -> None:
    """A cash finding heals only when the previous pass found it too (#574).

    The venue charges funding at the hour. The engine books it a moment later.
    A pass that reads the venue in that gap finds cash off by the payment. The
    engine books it before the next pass, so neither pass heals, and the mode
    guard is never asked.
    """
    store = SQLiteStore(":memory:")
    clock = ManualClock(7)
    keeper = _ledger(store, equity="100000", clock=clock)
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")
    venue = _AccountVenue(_held("99999.997238", ("BTC", "0.002", "0")))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    with capture_events() as first:
        asyncio.run(cycle.reconcile_account())
    keeper.checkpoint_funding(
        FundingAccrual(
            ts_event=3_600,
            ts_init=3_600,
            account_id=LIVE_ACCOUNT_ID,
            symbol="BTC",
            boundary_ts_ns=3_600,
            amount=Decimal("-0.002762"),
        )
    )
    clock.advance_to(8)
    with capture_events() as second:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == ()
    assert ledger.account().cash == Decimal("99999.997238")
    healed = [log for log in [*first, *second] if log["event"] == NamedEvent.ACCOUNT_HEALED.value]
    assert healed == []
    assert venue.mode_reads == 0
    record = _recorded(first)
    assert (record["tier_1"], record["unconfirmed"]) == (1, 1)


def test_a_gap_that_changed_size_between_passes_is_not_healed() -> None:
    """Two passes confirm a finding only if they find the same gap (#574).

    Each pass reads the venue while a different venue-first fill is still
    unbooked. Both find a BTC size gap, but not the same one. So neither pass
    is a second look at one gap, and neither heals.
    """
    store = SQLiteStore(":memory:")
    clock = ManualClock(7)
    keeper = _ledger(store, equity="100000", clock=clock)
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")
    venue = _AccountVenue(
        _held("100000", ("BTC", "0.003", "0")), _held("100000", ("BTC", "0.004", "0"))
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    asyncio.run(cycle.reconcile_account())
    clock.advance_to(8)
    with capture_events() as second:
        asyncio.run(cycle.reconcile_account())

    assert ledger.account_net() == {"BTC": Decimal("0.002")}
    assert [log for log in second if log["event"] == NamedEvent.ACCOUNT_HEALED.value] == []
    record = _recorded(second)
    assert (record["tier_1"], record["unconfirmed"]) == (1, 1)


def test_a_real_gap_still_heals_when_both_sides_trade_between_passes() -> None:
    """A finding is the gap, not the two figures (#574).

    The account is 500 cash and 0.001 BTC apart. Between the passes, both
    sides book the same 0.001 BTC fill with a fee of 1. Every figure moves,
    but both gaps stay where they were, so the second pass heals them. Keyed
    on the figures, a real gap would never heal while trading goes on.
    """
    store = SQLiteStore(":memory:")
    clock = ManualClock(7)
    keeper = _ledger(store, equity="100000", clock=clock)
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")
    venue = _AccountVenue(
        _held("100500", ("BTC", "0.003", "0")), _held("100499", ("BTC", "0.004", "0"))
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    asyncio.run(cycle.reconcile_account())
    _book_fill(ledger, quantity="0.001", price="64809", seq=2, fee="1")
    clock.advance_to(8)
    with capture_events() as second:
        asyncio.run(cycle.reconcile_account())

    assert ledger.account_net() == {"BTC": Decimal("0.004")}
    assert ledger.account().cash == Decimal("100499")
    healed = [log for log in second if log["event"] == NamedEvent.ACCOUNT_HEALED.value]
    assert len(healed) == 2
    record = _recorded(second)
    assert (record["tier_1"], record["unconfirmed"]) == (2, 0)


def test_a_gap_a_pass_healed_does_not_confirm_the_same_gap_on_the_next_pass() -> None:
    """A healed gap is closed, so the next pass does not count it as seen (#574).

    Two passes find BTC 0.001 short and the second heals it. Then a new
    venue-first fill of the same 0.001 is still unbooked at the third pass.
    That gap is new. Healing it would count the fill twice once the engine
    books it.
    """
    store = SQLiteStore(":memory:")
    clock = ManualClock(7)
    keeper = _ledger(store, equity="100000", clock=clock)
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")
    venue = _AccountVenue(
        _held("100000", ("BTC", "0.003", "0")),
        _held("100000", ("BTC", "0.003", "0")),
        _held("100000", ("BTC", "0.004", "0")),
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    asyncio.run(cycle.reconcile_account())
    clock.advance_to(8)
    asyncio.run(cycle.reconcile_account())
    assert ledger.account_net() == {"BTC": Decimal("0.003")}
    # A fresh stamp, so a heal here would be a new key and not a deduped retry.
    clock.advance_to(9)
    with capture_events() as third:
        asyncio.run(cycle.reconcile_account())

    assert ledger.account_net() == {"BTC": Decimal("0.003")}
    assert [log for log in third if log["event"] == NamedEvent.ACCOUNT_HEALED.value] == []
    record = _recorded(third)
    assert (record["tier_1"], record["unconfirmed"]) == (1, 1)


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
"""The tier-0 maintenance rate #152 measured. With it the ledger holds every margin
input, so a live ``None`` here is the venue's silence and not a missing spec."""

_BTC_CROSS_5X = LeverageBook(entries={"BTC": LeverageSpec(mode="cross", leverage=5)})
"""**Cross**, so the position has real backing on a live ledger.

Cross backs against account equity, which a materialised live ledger holds. An
isolated bucket is the venue's to post (ADR-0040 §3), and nothing here posts
one. So with cross the ledger holds every margin input, and a live ``None`` is
the venue's silence and not a missing input (#515)."""

_CROSS_5X = LeverageSpec(mode="cross", leverage=5)
"""``_BTC_CROSS_5X``'s pair, as the venue's own stored setting.

A snapshot handed to a cross-5x run has to be published at cross 5x, because
``margin_used`` is now compared and the two modes post by different rules: left
at the fixture's default isolated 1x, the venue would report the position's bare
uPnL against a ledger posting ``notional / 5`` — a divergence about the fixture's
two halves disagreeing (and a standing ``LEVERAGE_DIVERGENCE`` besides)."""

_BTC_5X_FREE = "0.0004"
"""The free margin a venue holding #142's account at this leverage publishes.

``account_state``'s default assumes the default isolated 1x, where an account
posts its positions' own buckets; these cases run the ledger at cross 5x, where
it posts ``notional / 5`` instead — ``0.002 × 64815 / 5 = 25.926`` against an
equity of ``25.9264``. Passed rather than left to the default so the snapshot is
one *this* account could have returned: a fixture disagreeing on a field the
case says nothing about would leave every one of them carrying a free-margin
finding."""

_BTC_5X_MAINTENANCE = "1.6203750"
"""The ``crossMaintenanceMarginUsed`` a venue holding that same account publishes.

``CROSSLESS_MAINTENANCE`` is the default because both fixtures build their rows
isolated, and these cases ``_levered`` theirs to cross — which puts the position
inside the venue's cross-scoped field for the first time. The figure is this
book's own exposure at #152's tier-0 rate, ``0.002 × 64815 × 0.0125``, and it is
passed for the same reason ``_BTC_5X_FREE`` is: the subject here is the
liquidation price, and a snapshot disagreeing on maintenance would leave each of
these cases carrying a Tier-2 finding about something else."""


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
    step aside for it. Before the first read, live has no number and reads
    `None`, never the formula's (#515).

    The account is #142's own — 0.002 BTC long from 64809 against an equity of
    25.9264. The ledger is given both margin-model inputs, so the `None` before
    the cycle is not a missing input. The snapshot agrees with the ledger on
    every Tier-1 line, so the cycle heals nothing and the only thing it leaves
    behind is the cache under test.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="25.9144", leverage=_BTC_CROSS_5X, specs={"BTC": _BTC_SPEC})
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "64815")
    venue = _AccountVenue(
        _levered(
            _priced(
                account_state(
                    "25.9264", "0.012", free_margin=_BTC_5X_FREE, maintenance=_BTC_5X_MAINTENANCE
                ),
                _BTC_LIQUIDATION,
            ),
            _CROSS_5X,
        )
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    before = projection.position("BTC", strategy_id="alpha")
    assert before is not None
    assert before.liquidation_price is None

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
    venue = _AccountVenue(
        _levered(
            _priced(
                account_state(
                    "25.9264", "0.012", free_margin=_BTC_5X_FREE, maintenance=_BTC_5X_MAINTENANCE
                ),
                _BTC_LIQUIDATION,
            ),
            _CROSS_5X,
        )
    )
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
    pins is the one live spends most of its time on. Falling back to the formula
    would report a price for a position the venue says has none, which is the
    fabricated Tier-2 value ADR-0034's freeze-never-substitute rule exists to
    refuse.

    The first cycle reports a price, so the second cycle's `None` has a real
    number to replace. Live reads `None` before any read lands too (#515).

    The same account and snapshot as the cache case above, so the only thing
    differing is the field under test.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="25.9144", leverage=_BTC_CROSS_5X, specs={"BTC": _BTC_SPEC})
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "64815")
    state = account_state(
        "25.9264", "0.012", free_margin=_BTC_5X_FREE, maintenance=_BTC_5X_MAINTENANCE
    )
    venue = _AccountVenue(
        _levered(_priced(state, _BTC_LIQUIDATION), _CROSS_5X),
        _levered(_priced(state, None), _CROSS_5X),
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)
    assert asyncio.run(cycle.reconcile_account()) == ()

    before = projection.position("BTC", strategy_id="alpha")
    assert before is not None
    assert before.liquidation_price == _BTC_LIQUIDATION

    assert asyncio.run(cycle.reconcile_account()) == ()

    after = projection.position("BTC", strategy_id="alpha")
    assert after is not None
    assert after.liquidation_price is None


def test_a_position_opened_since_the_read_reads_none_rather_than_the_formula() -> None:
    """A symbol the snapshot never mentioned is *also* an absent venue price.

    Live reads the venue's price for the whole book, not only for the symbols a
    cycle happened to carry. So an ETH position opened between deadlines reads
    ``None`` until the next pull prices it. It is never handed to the formula
    (#515). Otherwise one name would carry two different quantities, depending
    on whether a symbol made it into the last snapshot.

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
    venue = _AccountVenue(
        _levered(
            _priced(
                account_state(
                    "25.9264", "0.012", free_margin=_BTC_5X_FREE, maintenance=_BTC_5X_MAINTENANCE
                ),
                _BTC_LIQUIDATION,
            ),
            _CROSS_5X,
        )
    )
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

_ISOLATED_5X = LeverageSpec(mode="isolated", leverage=5)
"""``_BTC_ISOLATED_5X``'s pair, as the venue's own stored setting.

The mode whose ``margin_used`` the leverage does *not* divide, held at a leverage
that is not 1 so the two candidate band references come apart: at isolated 1x
``notional`` and ``notional / L`` are the same number and a case run there cannot
tell which one it was banded by."""

_BTC_BUCKET = Decimal("25.898067")
"""The locked collateral #142 measured on a funded isolated position, recovered
by the adapter as ``marginUsed − unrealizedPnl`` (ADR-0043 §3)."""


_BTC_DEFAULT = LeverageBook(entries={"BTC": DEFAULT_LEVERAGE})
"""BTC **declared** at the pair an unconfigured traded symbol resolves to.

Not ``EMPTY_LEVERAGE_BOOK``, and the difference is the whole of what ``declared``
reports: both books answer ``leverage_for("BTC")`` with the same
``1x``/``isolated``, one because the resolution filled it in over a symbol a
strategy trades and one because the read missed a book that carries nothing. A
drift case meaning the first must not be written on the second, or it asserts the
fallback while claiming to describe a run."""


def _drifted(logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """Every ``leverage.divergence`` in ``logs``, as the pairs it is about.

    A list, like ``_alerts``: the check ranges over every held symbol, and an
    operator who re-margined a book in the venue UI moved more than one of them.
    """
    return [
        {
            key: log[key]
            for key in (
                "symbol",
                "configured_mode",
                "configured_leverage",
                "declared",
                "venue_mode",
                "venue_leverage",
            )
        }
        for log in logs
        if log["event"] == NamedEvent.LEVERAGE_DIVERGENCE.value
    ]


def _levered(
    state: VenueAccountState, spec: LeverageSpec, *, symbol: str | None = None
) -> VenueAccountState:
    """The same snapshot with ``spec`` as the venue's stored setting — an
    operator having re-margined the book in the UI.

    Every position by default. ``symbol`` narrows it to one, which is what a
    case about the *other* symbols needs: an operator edits a book one position
    at a time, so a snapshot where the whole roster moved together cannot show
    that the untouched rows stayed quiet.

    The row's ``margin_used`` moves with the setting because it is derived from
    it: the two modes post by different rules and cross divides by the leverage,
    so a snapshot re-margined without it claims a pair it does not post at.
    Which is *not* the claim ADR-0044 §10 makes about a live account — a
    leverage change never re-margins an open position — but this helper builds
    the venue's steady state rather than the instant after an edit, and a
    fixture that held both would be inventing a third.

    The **bucket** moves with the setting for the same reason and is the other
    half of that steady state: a row's mode is written on it twice, and a cross
    position posts no bucket at all while an isolated one posts the bucket its
    margin is computed from (``UNPOSTED_BUCKET`` here, the bucket
    ``account_state`` builds). Left where it was, a snapshot
    re-margined to cross would carry an isolated position's bucket beside a
    cross setting, which ``margined`` now refuses rather than silently pricing
    off whichever field it read first.
    """

    def remargined(position: VenuePositionState) -> VenuePositionState:
        return margined(
            replace(
                position,
                leverage=spec,
                isolated_collateral=(
                    None
                    if spec.mode == "cross"
                    else (
                        UNPOSTED_BUCKET
                        if position.isolated_collateral is None
                        else position.isolated_collateral
                    )
                ),
            )
        )

    return replace(
        state,
        positions=tuple(
            remargined(p) if symbol is None or p.symbol == symbol else p for p in state.positions
        ),
    )


def test_a_venue_leverage_that_no_longer_matches_config_is_alerted_with_both_pairs() -> None:
    """Post-boot drift: a direct exact-match check against config, never a band
    and never a re-push (ADR-0044 §10).

    The indirect route is blind here, which is why the check exists at all: a
    leverage change never re-margins an open position, so neither term of the
    venue's ``margin_used`` moves with the *setting* and a drift in it is
    invisible in every computed figure the band already watches.

    Config is the run's default pair — ``1x``/``isolated``, what the boot push
    put on the venue — and the snapshot comes back at ``5x``/``cross``, the
    shape of a hand-edit in the venue UI. Both sides ride the record for the
    reason ``account.healed`` carries both: the operator who made the change is
    being told which of the two settings the engine will keep computing against,
    and a record naming only the venue's leaves that unanswered.

    ``declared`` is ``True`` here because BTC is in the run's resolved book: the
    pair on our side is one the boot push actually put on the venue, so the
    disagreement is between two settings that were both meant to apply.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000", leverage=_BTC_DEFAULT)
    _book_fill(keeper.portfolio, quantity="0.002", price="64809")
    venue = _levered(_held("100000", ("BTC", "0.002", "0")), LeverageSpec(mode="cross", leverage=5))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        asyncio.run(cycle.reconcile_account())

    assert _drifted(logs) == [
        {
            "symbol": "BTC",
            "configured_mode": "isolated",
            "configured_leverage": 1,
            "declared": True,
            "venue_mode": "cross",
            "venue_leverage": 5,
        }
    ]


def test_a_venue_margin_mode_that_no_longer_matches_config_is_alerted_on_its_own() -> None:
    """The comparison is on the **pair**, so a mode-only edit is drift too.

    The two settings ride one signed ``updateLeverage`` action and are one value
    here for that reason (ADR-0044 §2), so a check that watched the multiplier
    alone would read this venue as aligned — and it is not: at ``5x`` the switch
    from isolated to cross moves the position off its own locked bucket onto the
    account pool, so a loss on it now draws down every other position's margin.
    That is the change an operator is least likely to have made on purpose and
    the one a number-only comparison is blindest to.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000", leverage=_BTC_ISOLATED_5X)
    _book_fill(keeper.portfolio, quantity="0.002", price="64809")
    venue = _levered(_held("100000", ("BTC", "0.002", "0")), LeverageSpec(mode="cross", leverage=5))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        asyncio.run(cycle.reconcile_account())

    assert _drifted(logs) == [
        {
            "symbol": "BTC",
            "configured_mode": "isolated",
            "configured_leverage": 5,
            "declared": True,
            "venue_mode": "cross",
            "venue_leverage": 5,
        }
    ]


def test_a_symbol_still_carrying_the_configured_pair_is_not_alerted() -> None:
    """The check is per symbol, and silence is the aligned symbol's outcome.

    Both rows come back at the run's default pair — ``1x``/``isolated``, what
    the boot push put on the venue — and only ETH is then re-margined, the shape
    of an operator de-risking one position rather than the book. BTC is the
    assertion: an account that drifted somewhere must not be reported as having
    drifted everywhere, or the record stops naming which position an operator
    has to go look at.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(
        store,
        equity="100000",
        leverage=LeverageBook(entries={"BTC": DEFAULT_LEVERAGE, "ETH": DEFAULT_LEVERAGE}),
    )
    _book_fill(keeper.portfolio, quantity="0.002", price="64809")
    _book_fill(keeper.portfolio, quantity="1.5", price="3000", symbol="ETH", seq=2)
    venue = _levered(
        _held("100000", ("BTC", "0.002", "0"), ("ETH", "1.5", "0")),
        LeverageSpec(mode="cross", leverage=5),
        symbol="ETH",
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        asyncio.run(cycle.reconcile_account())

    assert _drifted(logs) == [
        {
            "symbol": "ETH",
            "configured_mode": "isolated",
            "configured_leverage": 1,
            "declared": True,
            "venue_mode": "cross",
            "venue_leverage": 5,
        }
    ]


def test_a_venue_symbol_outside_the_book_is_alerted_as_undeclared_rather_than_as_config() -> None:
    """The engine's side of this comparison is not always something an operator
    wrote, and the record has to say which.

    The check ranges over the **venue's** rows (ADR-0044 §10), so it reaches a
    symbol no configured strategy trades — a position left over from an earlier
    run, or one opened by hand in the venue UI. ``leverage_for`` answers those
    with ``DEFAULT_LEVERAGE``, because a read may never be more permissive than
    the book, and that answer is *not* a fabrication: the Tier-1 size finding
    beside this one heals the row into the ledger, and ``domain.valuation``
    then computes its margin at exactly that fallback pair. So the disagreement
    is real and alerting is right — silence here would hide a position the model
    values at ``1x`` isolated while the venue holds it at ``20x`` cross.

    What would be wrong is the *word*. ``configured_mode``/``configured_leverage``
    read as a pair an operator chose, and for this symbol nobody chose one; an
    operator told "your config says 1x" would go looking through a config that
    never mentions SOL. ``declared`` is the one bit that separates the two, and
    it changes the advice: a declared drift is two settings that were both meant
    to apply and one of them moved, an undeclared one is a position this run
    does not trade and never pushed a leverage for.

    BTC is in the same snapshot and stays silent, so the flag is attributable to
    the symbol rather than to the pass.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000", leverage=_BTC_DEFAULT)
    _book_fill(keeper.portfolio, quantity="0.002", price="64809")
    venue = _levered(
        _held("100000", ("BTC", "0.002", "0"), ("SOL", "40", "0")),
        LeverageSpec(mode="cross", leverage=20),
        symbol="SOL",
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert _drifted(logs) == [
        {
            "symbol": "SOL",
            "configured_mode": "isolated",
            "configured_leverage": 1,
            "declared": False,
            "venue_mode": "cross",
            "venue_leverage": 20,
        }
    ]
    # The companion finding ADR-0044 §10 leans on when it declines to narrow the
    # range to the book: the symbol is already reported as a size disagreement,
    # and it is that heal which puts the position under the fallback pair.
    assert divergences is not None
    assert ("SOL", DivergenceField.SIGNED_SIZE) in {
        (divergence.symbol, divergence.field) for divergence in divergences
    }


def test_a_cycle_that_finds_only_leverage_drift_writes_nothing_and_cannot_re_push() -> None:
    """Drift is **reported and never corrected** (ADR-0044 §10): an operator who
    lowered a leverage in the venue UI to de-risk a live position must not be
    silently reverted, and neither side of the disagreement is written down.

    Two writes to refuse, one per side. Toward the **store**, config is not
    truth to be persisted: the map is already the run's own input, so a cycle
    that wrote anything here would be recording a venue setting it does not use
    or re-deriving a figure from a leverage it just learned had changed. Toward
    the **venue**, a re-push is the revert itself.

    The venue agrees on every figure it is compared on, so the drift is the
    pass's only finding and the untouched store is attributable to it alone.

    The refusal to re-push is asserted as the **shape of the seam** rather than
    as an uncalled method: the cycle is constructed against ``AccountAnchor``,
    whose members are a snapshot read, a mode verdict, an open-order list
    (ADR-0052), and a position read (ADR-0054). All four are reads, so there is no write
    to leave uncalled and a case cannot fail by forgetting to check. Pinning the
    member set is what keeps that true — a later ``update_leverage`` added here
    for a caller elsewhere would hand this cycle the revert it must not have.
    """
    assert {name for name in dir(AccountAnchor) if not name.startswith("_")} == {
        "fetch_account_state",
        "fetch_open_orders",
        "fetch_positions",
        "verify_account_mode",
    }

    store = _SealedStore(":memory:")
    keeper = _ledger(store, equity="100000", leverage=_BTC_DEFAULT)
    _book_fill(keeper.portfolio, quantity="0.002", price="64809")
    _posted(keeper, ("BTC", "0.002", "0"))
    opened = store.load_account()
    stored = store.all_positions()
    assert opened is not None
    venue = _levered(_held("100000", ("BTC", "0.002", "0")), LeverageSpec(mode="cross", leverage=5))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)
    store.seal()

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert _drifted(logs) == [
        {
            "symbol": "BTC",
            "configured_mode": "isolated",
            "configured_leverage": 1,
            "declared": True,
            "venue_mode": "cross",
            "venue_leverage": 5,
        }
    ]
    assert divergences == ()  # the drift is not one, and nothing else disagreed

    restored = store.load_account()
    assert restored is not None
    assert restored.cash == opened.cash == Decimal("100000")
    assert store.all_positions() == stored


def test_a_paper_run_checks_no_leverage_because_it_has_no_venue_to_check() -> None:
    """The check is live-only, and on paper it is not skipped by a flag — there
    is nothing to compare against.

    ``PaperExchange`` satisfies ``AccountAnchor`` and answers it permanently
    with ``None``: that venue holds no second account, so the cycle freezes at
    the read and never reaches a classification, let alone a leverage pair. A
    branch on the run mode would say the same thing worse, putting a live/paper
    conditional inside the cycle for a case its ``None`` contract already covers.

    The config here is deliberately **not** the default pair, so the silence is
    a real outcome rather than an accident of the two sides matching: at ``5x``
    cross there is a disagreement available for any check that ran.
    """
    store = SQLiteStore(":memory:")
    keeper = Checkpointer(
        spec=AccountSpec(account_id="paper-default", genesis_collateral=Decimal("100000")),
        store=store,
        clock=ManualClock(7),
        leverage=_BTC_CROSS_5X,
    )
    keeper.recover()
    _book_fill(keeper.portfolio, quantity="0.002", price="64809")
    venue = PaperExchange(
        bus=InMemoryBus(),
        clock=ManualClock(7),
        fill_model=ImmediateFillModel(),
        genesis_collateral=Decimal("100000"),
        account_net=dict,
        applied_fills=lambda cloid: (),
        resting_orders=list,
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is None  # frozen at the read: no venue truth, no pass
    assert _drifted(logs) == []
    # And the silence is not the config's doing: the run really is on a pair a
    # check would have had something to say about.
    assert keeper.portfolio.leverage_for("BTC") == LeverageSpec(mode="cross", leverage=5)


def _isolated(
    state: VenueAccountState, collateral: Decimal, *, margin_used: str | None = None
) -> VenueAccountState:
    """The same snapshot with ``collateral`` on every position it carries, and
    the ``margin_used`` that bucket backs.

    A real venue's isolated position locks a positive bucket, and this is what
    posts one: ``account_state``'s rows carry ``UNPOSTED_BUCKET`` instead, so
    that cases about something else need no bucket arithmetic.

    A ``Decimal`` and not ``Decimal | None``. This used to take ``None`` for
    "the venue says **cross**, backed by the account pool" — the claim the
    adapter's own ``_isolated_collateral`` refuses to guess at — and promised
    ``margined`` would then post the row out of the pool. It never did: it
    branched on the row's ``leverage``, which this helper does not touch, so a
    ``None`` produced an isolated rule over an invented zero bucket instead. No
    caller ever passed one, so the paragraph documented a path that did not
    exist. A case wanting the venue's cross rule uses ``_levered``, which moves
    the setting and the bucket together.

    ``margin_used`` takes precedence and is the escape hatch, the one
    ``implied_free_margin`` holds for its own field: a case whose subject is the
    disagreement passes the figure it wants compared.

    ``free_margin`` moves with the posted margin. The venue takes the bucket
    out of it (#402 measured `843.53 = 928.27 − 84.73`), so a snapshot that
    posted a bucket and kept the unbucketed figure is one no venue returns.
    """

    def bucketed(position: VenuePositionState) -> VenuePositionState:
        posted = margined(replace(position, isolated_collateral=collateral))
        return posted if margin_used is None else replace(posted, margin_used=Decimal(margin_used))

    positions = tuple(bucketed(p) for p in state.positions)
    return replace(
        state,
        positions=positions,
        free_margin=state.equity - sum((p.margin_used for p in positions), Decimal("0")),
    )


def test_the_isolated_snapshot_carries_the_margin_a_venue_would_have_published() -> None:
    """An isolated position's ``marginUsed`` is its own bucket marked to market
    — ``isolated_collateral + unrealized_pnl`` (ADR-0040 §3) — and a fixture
    that posts a bucket has to move the figure that bucket backs.

    The recorded ``25.9168`` this replaces is the *cross* arithmetic of the
    snapshot it came from, ``positionValue / 5``, and it survives here only
    because nothing compared it. Compared, it is a snapshot no venue could have
    returned: the same row claims a bucket of ``25.898067`` and a margin that
    ignores it.

    The band would absorb the gap, but the band gates the alert and not the
    classification, so the incoherence would still be counted as a Tier-2
    divergence under every case here whose subject is the collateral ingest.
    """
    state = _isolated(account_state("25.9264", "0.012"), _BTC_BUCKET)

    assert [p.margin_used for p in state.positions] == [Decimal("25.910067")]


def test_a_row_whose_bucket_and_margin_mode_disagree_is_refused_rather_than_priced() -> None:
    """``margined`` will not price a row whose two mode signals contradict each
    other, because neither rule has an honest figure to give it.

    ``VenuePositionState`` carries the mode twice and the two have to agree:
    ``isolated_collateral`` is ``None`` **exactly** when the position is cross
    and backed by the account pool, while ``leverage`` is the venue's stored
    setting for the symbol. Either half alone decides which of ADR-0040 §3's two
    rules the row posts by, so a row that answers them differently is a snapshot
    no venue returns.

    It used to be priced anyway. The isolated arm read an absent bucket as
    ``0`` and published the bare unrealized PnL wearing the bucket's name —
    which on a losing position is a **negative** ``marginUsed``, a figure no
    venue has ever returned. Inventing the bucket made the contradiction free to
    write, so every fixture below had to be trusted not to express it; refused,
    it cannot reach a comparison at all.
    """
    row = account_state("25.9264", "-0.034").positions[0]

    with pytest.raises(ValueError, match="isolated row"):
        margined(replace(row, isolated_collateral=None))

    with pytest.raises(ValueError, match="cross row"):
        margined(replace(row, isolated_collateral=_BTC_BUCKET, leverage=_CROSS_5X))


def test_a_case_that_wants_the_isolated_margin_disagreement_declares_it() -> None:
    """The derivation is the default and not the only answer, on
    ``implied_free_margin``'s own asymmetry: a margin the venue disagrees on is
    a snapshot it could have returned — a stale bucket, a fee charged against it
    between the two reads — so a case whose subject *is* that disagreement says
    so rather than reaching for a second fixture.

    ``implied_notional`` takes no such hatch because an exposure contradicting
    the size and entry published beside it is not a snapshot any venue returns.
    """
    state = _isolated(account_state("25.9264", "0.012"), _BTC_BUCKET, margin_used="30")

    assert [p.margin_used for p in state.positions] == [Decimal("30")]


def test_a_cycle_ingests_the_venues_locked_collateral_onto_the_partition() -> None:
    """The bucket is the **venue's** to post on live, so it has to be read from
    the venue (ADR-0043 §3: "the reconcile still re-ingests it").

    Live never computes this number — ``_lock_isolated_collateral`` declines on
    the declared-versus-ingested predicate — so before the cycle the ledger's
    bucket is unposted, and the figures that need it read ``None`` until the
    cycle ingests it (#516, #522). Read as a real bucket, ``margin_used`` was the bare unrealized
    PnL and a 5x position read 10 802.5x.

    The snapshot agrees with the ledger on every Tier-1 line, so the cycle heals
    nothing and the ingest is the only thing it leaves behind. That the pass
    raises no margin finding over the missing bucket is #402's case below.
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
    # Unknown, because nothing has posted a bucket yet.
    assert before.margin_used is None
    assert before.effective_leverage is None

    asyncio.run(cycle.reconcile_account())

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


def test_a_bucket_the_ledger_has_not_ingested_yet_is_not_a_margin_divergence() -> None:
    """The first cycle over an isolated position agrees with the venue (#402).

    Live never computes the bucket. It learns it from this same snapshot. So a
    ledger that has not ingested it yet is not disagreeing with the venue. It
    is waiting for the number the pass is about to take. Compared before the
    ingest, `margin_used` and `free_margin` both diverged by the whole bucket,
    on every isolated position held across a reconcile.

    Ledger and venue agree on size, cash and uPnL, so the bucket is the only
    thing that could make this pass report anything.
    """
    keeper = _ledger(
        SQLiteStore(":memory:"),
        equity="25.9144",
        leverage=_BTC_ISOLATED_5X,
        specs={"BTC": _BTC_SPEC},
    )
    _book_fill(keeper.portfolio, quantity="0.002", price="64809")
    _mark(keeper.portfolio, "BTC", "64815")
    venue = _AccountVenue(_isolated(account_state("25.9264", "0.012"), _BTC_BUCKET))
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    assert asyncio.run(cycle.reconcile_account()) == ()


def _alerts(logs: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """Every ``valuation.divergence`` in ``logs``, as the four fields it is
    *about*, in the order the pass raised them.

    A list rather than the single-record unpacking ``_recorded`` does, because
    one mark skew moves every quantity it flows through: an account whose uPnL
    has drifted has an equity that has drifted with it, so a case that finds one
    alert and a case that finds two are both ordinary books.

    Projected down to the payload because the rendering furniture every record
    carries — the level, the run id — belongs to the observability suite's
    contract rather than to this cycle's, and a case here asserting on it would
    fail on a change to that one.
    """
    return [
        {key: log[key] for key in ("field", "symbol", "ledger", "venue")}
        for log in logs
        if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value
    ]


def test_a_tier_2_divergence_outside_the_band_is_alerted_with_both_sides() -> None:
    """The alert, and the pair it carries (ADR-0040 §6).

    Alert-only is the whole of Tier-2's response: the figure is recomputed from
    ``(position, mark)`` on every read and never stored, so there is nothing to
    heal — a stale mark is not a wrong ledger. What an operator gets instead is
    the same shape ``account.healed`` carries, both sides and never their
    difference, for the same reason: a delta cannot tell a mark this engine has
    not seen yet from a position it has mis-valued.

    The book is the fill's own arithmetic: a 0.002 long entered at 64809 and
    marked at 65000 is worth ``0.002 × 191 = 0.382``, on a 100000 cash line.
    The venue's snapshot is built to disagree on the valuation *only* — its
    equity carries its own uPnL exactly, so ``venue_cash`` lands back on 100000
    and the Tier-1 halves stay silent. That matters beyond tidiness here: a cash
    finding would suppress these very alerts (ADR-0040 §6's first rule), so a
    case that let one through would be asserting on a path it had disabled.

    All four figures alert because one mark skew moves all of them — equity *is*
    ``cash + Σ uPnL``, the exposure is priced off the same mark, and at the run's
    default isolated 1x the posted margin is the uPnL over again, no bucket
    having been ingested — and the account grain comes first, as the cash line
    does. The venue's 1.000 of uPnL puts its mark at 65309, so its
    ``positionValue`` is 130.618 against the ledger's 130.000, a gap the
    notional's own band of ``max(0.01, 0.001 × 130.000)`` does not absorb; the
    margin's band is narrower still, scaled by the ``130.000 / 1`` it posts.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [divergence.field for divergence in divergences] == [
        DivergenceField.EQUITY,
        DivergenceField.UNREALIZED_PNL,
        DivergenceField.NOTIONAL,
        DivergenceField.MARGIN_USED,
    ]
    assert _alerts(logs) == [
        {
            "field": "equity",
            "symbol": None,
            "ledger": "100000.382",
            "venue": "100001.000",
        },
        {
            "field": "unrealized_pnl",
            "symbol": "BTC",
            "ledger": "0.382",
            "venue": "1.000",
        },
        {
            "field": "notional",
            "symbol": "BTC",
            "ledger": "130.000",
            "venue": "130.6180",
        },
        {
            "field": "margin_used",
            "symbol": "BTC",
            "ledger": "0.382",
            "venue": "1.000",
        },
    ]


def test_a_tier_2_divergence_inside_the_band_is_not_alerted() -> None:
    """The band is a gate on the *alert*, not on the classification (ADR-0040 §6).

    The venue's mark is a robust median this engine does not replicate
    bit-for-bit, so a Tier-2 figure will never agree exactly and an unbanded
    alert would fire on every healthy cycle — which is the failure mode that
    makes an alert channel worth ignoring. What clears the band is absorbed
    silently.

    The same book behavior 1 alerts on, moved inside the tolerance: a 0.002 long
    entered at 64809 and marked at 65000 is worth ``0.002 x 191 = 0.382``, and
    the venue is built to report 0.385 — a 0.003 gap, under the 0.01 rounding
    floor. Its equity carries its own uPnL exactly, so ``venue_cash`` lands back
    on 100000 and neither Tier-1 half speaks; a cash finding would suppress
    these alerts by a different rule and the case would prove nothing. The
    exposure the same 0.003 implies is inside the floor by the same margin, and
    so is the margin it posts — which at this run's isolated 1x is the uPnL over
    again, so it is the identical 0.003.

    All four figures are still **classified**, and the case asserts that rather
    than only the silence: a band that dropped them at classification would pass
    an alert assertion identically while leaving the cycle's own record claiming
    the pass agreed, and leaving the heal path with nothing to read.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100000.385", ("BTC", "0.002", "0.385"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [divergence.field for divergence in divergences] == [
        DivergenceField.EQUITY,
        DivergenceField.UNREALIZED_PNL,
        DivergenceField.NOTIONAL,
        DivergenceField.MARGIN_USED,
    ]
    assert _alerts(logs) == []


def test_an_isolated_margin_is_banded_by_the_positions_notional_not_the_cross_divisor() -> None:
    """The second of ``margin_used``'s two references, and the mode split is the
    whole of it (ADR-0046 §5).

    A cross position posts ``notional / L`` out of the shared pool, so that is
    the notional a mark skew reaches its margin through. An **isolated** one
    posts a locked bucket marked to market — ``isolated_collateral + uPnL``,
    which no leverage divides — so the skew arrives at full exposure and the
    reference is the position's own ``notional``. Dividing it anyway makes the
    band ``L`` times too narrow on every isolated book, and narrowest at the
    leverage an operator reached for to hold a *larger* position: the alert
    channel then fires on the healthy cycles of exactly the accounts most able
    to generate them.

    Run at isolated **5x**, which is what makes the case discriminating: at the
    default 1x the two candidate references are the same number and the pass
    would be green under either rule.

    The book is the band cases' own — a 0.002 long entered at 64809 and marked
    at 65000, worth ``0.002 × 191 = 0.382`` on a 100000 cash line — with the
    venue built to sit 0.05 away on every mark-sensitive figure at once, which
    at this mode is one number appearing four times: the posted margin *is* the
    uPnL again, no bucket having been ingested, and the exposure and the equity
    carry the same 0.05. That gap is deliberately **between** the two references'
    bands: ``0.001 × 130.000 = 0.130`` absorbs it and ``0.001 × 26.000 = 0.026``
    does not, so a margin banded by the cross divisor is the only figure of the
    four that speaks.

    The venue's equity carries its own uPnL exactly, so ``venue_cash`` lands
    back on 100000 and neither Tier-1 half speaks — a cash finding would
    suppress these alerts by a different rule and the case would prove nothing.
    Its stored setting is the run's own, so nothing here is a leverage drift.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000", leverage=_BTC_ISOLATED_5X)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _levered(_held("100000.432", ("BTC", "0.002", "0.432")), _ISOLATED_5X)
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    # Still classified, all four: the band gates the alert and not the record.
    assert [divergence.field for divergence in divergences] == [
        DivergenceField.EQUITY,
        DivergenceField.UNREALIZED_PNL,
        DivergenceField.NOTIONAL,
        DivergenceField.MARGIN_USED,
    ]
    assert _alerts(logs) == []


def test_account_maintenance_margin_is_compared_against_the_venues_cross_scoped_field() -> None:
    """The sixth and last of ADR-0040 §6's banded quantities, and the one whose
    two sides are scoped differently (ADR-0046 §2.1).

    The venue publishes ``crossMaintenanceMarginUsed``, which counts cross
    positions and silently omits isolated ones — while ADR-0040 §2's account row
    is a Σ over every position. So the *comparison* narrows to the cross subset
    and the **reported** figure stays Σ-over-all: dropping the field from the
    band instead would discard the signal on exactly the positions the venue
    does publish a number for, and comparing the reported Σ would fire an
    unbounded gap on every account holding an isolated leg — the primary mode in
    practice, and not the mark skew the band was sized for.

    Account grain, so the record carries no symbol, and it sits with the equity
    and free-margin findings ahead of the per-symbol ones. Alerted and never
    healed, with the rest of the tier.

    A cross-only book here, so the subset *is* the whole roster and the case is
    about the comparison existing at all rather than about its scope. The 0.002
    long marked at 65000 is worth 130.000, and #152's tier-0 rate puts the
    ledger's maintenance at ``130.000 × 0.0125 = 1.625000``; the venue is built
    to publish 1.400, a gap of 0.225 that the band's ``0.001 × 130.000`` does
    not absorb. Every other figure is built to agree — the snapshot's equity
    carries its own uPnL exactly, so ``venue_cash`` lands back on 100000 and no
    Tier-1 finding is there to suppress the alert.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000", leverage=_BTC_CROSS_5X, specs={"BTC": _BTC_SPEC})
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _levered(
        # Free margin is the cross pair's difference at this run's own leverage:
        # 100000.382 of equity less the 130.000 / 5 the position posts.
        _held(
            "100000.382",
            ("BTC", "0.002", "0.382"),
            free_margin="99974.382",
            maintenance="1.400",
        ),
        _CROSS_5X,
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [(d.field, d.symbol, d.ledger, d.venue) for d in divergences] == [
        (DivergenceField.MAINTENANCE_MARGIN, None, Decimal("1.6250000"), Decimal("1.400"))
    ]
    assert _alerts(logs) == [
        {
            "field": "maintenance_margin",
            "symbol": None,
            "ledger": "1.625",
            "venue": "1.400",
        }
    ]


_ETH_SPEC = InstrumentSpec(
    symbol="ETH",
    sz_decimals=4,
    max_decimals=6,
    min_notional=Decimal("0"),
    margin_maint=Decimal("0.0125"),
)
"""A second instrument carrying the same rate as ``_BTC_SPEC``.

Given a rate rather than left specless so that the case below excludes its
symbol on the **mode** the narrowing is about, not on a maintenance term that
was ``None`` anyway: a subset test whose out-of-subset leg could not have been
summed proves nothing about the subset."""


def test_a_large_isolated_book_does_not_widen_the_cross_subsets_maintenance_band() -> None:
    """``maintenance_margin`` is the one row of ADR-0046 §5's table whose
    reported and compared quantities differ, and the reference follows the
    **compared** one.

    §2.1 narrows the comparison to the cross subset because the venue's
    ``crossMaintenanceMarginUsed`` omits isolated positions. Left at the band's
    account-grain default — the Σ over every notional — the divisor would still
    be the whole book, so the band widens by ``Σ_all / Σ_cross``: unbounded, and
    largest on exactly the accounts that hold a big isolated leg beside a small
    cross one. That is the primary shape in practice, and it suppresses the
    tier-crossing signal ADR-0040 §4 says the alert exists to raise.

    So the book here is deliberately lopsided: 0.002 BTC **cross 5x** marked at
    65000 is 130.000 of exposure, against 100 ETH **isolated** at 3000 worth
    300000 — a roster where the cross subset is 0.04 % of the notional. The
    ledger's cross maintenance is ``130.000 × 0.0125 = 1.6250000`` and the venue
    publishes 1.600, a gap of 0.025. Against the cross-subset reference the band
    is ``max(0.01, 0.001 × 1.6250000) = 0.01`` and the gap speaks; against the
    whole book's notional it is ``0.001 × 300130 = 300.13`` and a divergence
    over fifteen times the figure itself would stay silent.

    The ETH leg carries a rate of its own, so its 3750 of maintenance is a term
    the ledger *could* have summed and does not — the subset is the config's
    margin mode, not whichever symbol happened to be priceable. Every other
    figure is built to agree: the snapshot's equity carries its own uPnL exactly
    so ``venue_cash`` lands back on 100000, the ETH leg is entered at its own
    mark so it is flat on both sides, and the free margin is the cross pair's
    difference at this run's leverage.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(
        store,
        equity="100000",
        leverage=LeverageBook(entries={"BTC": _CROSS_5X, "ETH": DEFAULT_LEVERAGE}),
        specs={"BTC": _BTC_SPEC, "ETH": _ETH_SPEC},
    )
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _book_fill(projection, quantity="100", price="3000", symbol="ETH")
    _mark(projection, "BTC", "65000")
    _mark(projection, "ETH", "3000")
    venue = _levered(
        _held(
            "100000.382",
            ("BTC", "0.002", "0.382"),
            ("ETH", "100", "0"),
            entry={"ETH": "3000"},
            free_margin="99974.382",
            maintenance="1.600",
        ),
        _CROSS_5X,
        symbol="BTC",
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [(d.field, d.symbol, d.ledger, d.venue) for d in divergences] == [
        (DivergenceField.MAINTENANCE_MARGIN, None, Decimal("1.6250000"), Decimal("1.600"))
    ]
    assert _alerts(logs) == [
        {
            "field": "maintenance_margin",
            "symbol": None,
            "ledger": "1.625",
            "venue": "1.600",
        }
    ]


def test_free_margin_is_classified_at_tier_2_against_the_venues_own_figure() -> None:
    """The third account-grain Tier-2 figure, and the one an operator sizes
    against (ADR-0040 §6).

    Mark-dependent through both its terms — ``equity − total_margin_used``, and
    at the default isolated 1x the margin term is the locked bucket marked to
    market — so it belongs to the tier that is recomputed and alerted rather
    than the one that is healed. The venue publishes its own, which is what
    makes it comparable at all: ``effective_leverage`` beside it has no venue
    field and is therefore reported and never cross-checked.

    Deliberately **not** compared against the venue's ``withdrawable``, which
    additionally deducts margin reserved by resting orders — the normal state of
    a running engine, and a gap no band absorbs (ADR-0046 §2). The adapter's
    figure is the cross pair's difference.

    The book is built so free margin is the pass's *only* finding: a 0.002 long
    entered at 64809 and marked at 65000 carries ``0.002 x 191 = 0.382`` of
    uPnL, which the venue's snapshot reports exactly and which its equity
    carries exactly, so cash, size, equity and uPnL all agree. The ledger's free
    margin is then ``100000.382 − 0.382 = 100000`` — the bucket behind an
    isolated position is its own uPnL and nothing else, since a fill locks no
    collateral until a cycle ingests one — against a venue reporting 99000.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100000.382", ("BTC", "0.002", "0.382"), free_margin="99000")
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == (
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.FREE_MARGIN,
            symbol=None,
            ledger=Decimal("100000.000"),
            venue=Decimal("99000"),
        ),
    )
    assert _alerts(logs) == [
        {
            "field": "free_margin",
            "symbol": None,
            "ledger": "100000",
            "venue": "99000",
        }
    ]


def test_a_near_zero_free_margin_in_a_levered_book_is_not_alerted_on() -> None:
    """The band's relative term scales by the **notional** the figure's
    mark-sensitivity flows through, never by the figure being compared
    (ADR-0046 §5).

    Free margin is the quantity that makes the difference load-bearing: it goes
    through zero on a fully-deployed account, so a band scaled by the compared
    value collapses onto ``atol`` exactly where the book is largest, and every
    healthy cycle on a levered account alerts. Scaling by equity — #142's
    proposal — is the same defect one step milder: at leverage ``L`` equity is
    roughly ``notional / L``, so the band comes out ``L`` times too tight on
    precisely the leveraged positions this surface exists to model.

    The book is deployed to the last dollar: a 20 BTC long entered and marked at
    50000 is 1000000 of notional against a 100000 cash line, and at cross 10x
    its margin is ``1000000 / 10 = 100000`` — the whole of equity, leaving a
    free margin of exactly ``0``. The venue publishes ``0.5``. That gap is 50x
    the rounding floor and would alert on any self-scaled band, and it is 1/2000
    of ``rtol x 1000000`` on the notional-scaled one, which is the band a real
    mark skew is measured against.

    Marked at entry so the pass has one finding: uPnL is a real zero on both
    sides, so ``venue_cash`` lands back on the cash line and equity, size and
    uPnL all agree. The snapshot is published at the ledger's own pair for the
    same reason — a venue holding this book at the fixture's default 1x would
    post the whole notional as margin and disagree on a field the case says
    nothing about. The divergence is still **classified** — the band gates the
    alert and never the finding — which the case pins beside the silence.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(
        store,
        equity="100000",
        leverage=LeverageBook(entries={"BTC": LeverageSpec(mode="cross", leverage=10)}),
    )
    projection = keeper.portfolio
    _book_fill(projection, quantity="20", price="50000")
    _mark(projection, "BTC", "50000")
    venue = _levered(
        _held("100000", ("BTC", "20", "0"), entry={"BTC": "50000"}, free_margin="0.5"),
        LeverageSpec(mode="cross", leverage=10),
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences == (
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.FREE_MARGIN,
            symbol=None,
            ledger=Decimal("0"),
            venue=Decimal("0.5"),
        ),
    )
    assert _alerts(logs) == []


def test_a_tier_1_size_finding_suppresses_that_symbols_tier_2_alert() -> None:
    """ADR-0040 §6's first suppression: a valuation computed on a book the cycle
    has just found wrong is not a second finding.

    The healed ledger is the root cause. A symbol whose size disagrees has a
    uPnL that disagrees *because* of it, so alerting on both reports one missed
    fill twice — once in contracts and once in dollars — and the dollar half is
    the one an operator cannot act on. Tier-1 already alerts and heals, so
    nothing is lost by the silence.

    Scoped to the symbol, which is why this case holds two of them. A blanket
    "any Tier-1 finding silences Tier-2" would pass an assertion that only looked
    for the absence, while quietly deafening the account to every other symbol on
    the book for the rest of the cycle.

    The book: a 0.002 BTC long entered at 64809 and marked at 65000 is worth
    ``0.002 x 191 = 0.382`` against a venue holding **0.003** and pricing it at
    50.382; a 1 ETH long entered at 3000 and marked at 3100 is worth 100 against
    a venue agreeing on the size and pricing it at 50. The two Tier-2 gaps are
    equal and opposite by construction, so ``Σ uPnL`` agrees, and with it the
    cash line the venue's equity implies, the equity itself and the free margin —
    leaving the account grain silent for its own reasons rather than by a
    suppression this case is not testing.

    Both gaps are 50 against bands of 0.13 (BTC's 130 of notional) and 3.10
    (ETH's 3100), so each would alert on its own. Only ETH's does.

    The exposures diverge with them and are suppressed on the same grain: the
    venue's ETH is marked at 3050 where the ledger has 3100, and its BTC at
    81603 on a size the ledger does not hold at all — the second of which is the
    missed fill restated a third time, in exposure, and silenced with the rest of
    BTC's Tier-2 half. The posted margins are a fourth restatement — this run is
    at the default isolated 1x with no bucket ingested, so each is its symbol's
    uPnL — and they split the same way, BTC's silenced and ETH's alerted.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _book_fill(projection, quantity="1", price="3000", symbol="ETH", seq=2)
    _mark(projection, "BTC", "65000")
    _mark(projection, "ETH", "3100")
    venue = _held(
        "100100.382", ("BTC", "0.003", "50.382"), ("ETH", "1", "50"), entry={"ETH": "3000"}
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
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
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.UNREALIZED_PNL,
            symbol="BTC",
            ledger=Decimal("0.382"),
            venue=Decimal("50.382"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.UNREALIZED_PNL,
            symbol="ETH",
            ledger=Decimal("100"),
            venue=Decimal("50"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.NOTIONAL,
            symbol="BTC",
            ledger=Decimal("130.000"),
            venue=Decimal("244.8090"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.NOTIONAL,
            symbol="ETH",
            ledger=Decimal("3100"),
            venue=Decimal("3050"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.MARGIN_USED,
            symbol="BTC",
            ledger=Decimal("0.382"),
            venue=Decimal("50.382"),
        ),
        Divergence(
            tier=DivergenceTier.TIER_2,
            field=DivergenceField.MARGIN_USED,
            symbol="ETH",
            ledger=Decimal("100"),
            venue=Decimal("50"),
        ),
    )
    assert _alerts(logs) == [
        {"field": "unrealized_pnl", "symbol": "ETH", "ledger": "100", "venue": "50"},
        {"field": "notional", "symbol": "ETH", "ledger": "3100", "venue": "3050"},
        {"field": "margin_used", "symbol": "ETH", "ledger": "100", "venue": "50"},
    ]


def test_a_tier_1_size_finding_suppresses_the_account_figures_its_symbol_is_a_term_of() -> None:
    """ADR-0040 §6 rule (1), as corrected by #322: a size finding on a symbol
    explains every Tier-2 figure whose compared Σ contains that symbol.

    The case above builds two opposite gaps so the account Σs agree by
    construction. This is the case it stepped around. One symbol, one gap: the
    ledger holds 0.002 BTC, the venue holds 0.003, and cash agrees. The equity
    gap is then 50, which is the missed 0.001 BTC at the mark restated in
    dollars. Tier-1 alerts and heals that fill, and the next pass finds the
    cash gap as Tier-1, so an equity alert here is one an operator cannot act
    on.

    Before this fix the rule matched on the grain alone. The size finding
    carries ``symbol="BTC"`` and equity carries ``symbol=None``, so the three
    per-symbol figures were silenced and the equity alerted anyway.

    Suppression by a Tier-1 finding is not counted, so ``suppressed`` stays 0.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100050.382", ("BTC", "0.003", "50.382"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [
        (divergence.tier, divergence.field, divergence.symbol) for divergence in divergences
    ] == [
        (DivergenceTier.TIER_1, DivergenceField.SIGNED_SIZE, "BTC"),
        (DivergenceTier.TIER_2, DivergenceField.EQUITY, None),
        (DivergenceTier.TIER_2, DivergenceField.UNREALIZED_PNL, "BTC"),
        (DivergenceTier.TIER_2, DivergenceField.NOTIONAL, "BTC"),
        (DivergenceTier.TIER_2, DivergenceField.MARGIN_USED, "BTC"),
    ]
    assert _alerts(logs) == []
    record = _recorded(logs)
    assert (record["tier_1"], record["tier_2"], record["suppressed"]) == (1, 4, 0)


def test_a_size_finding_on_an_isolated_symbol_does_not_silence_the_cross_maintenance_alert() -> (
    None
):
    """The narrowing that makes rule (1) one decision with rule (2) (#322).

    A size finding silences the figures whose compared Σ contains its symbol,
    and ``maintenance_margin`` is compared over the cross subset only
    (ADR-0046 §2.1). An isolated symbol is not a term of that Σ, so a size gap
    on it explains nothing about the maintenance figure. A blanket "any size
    finding silences the account grain" would pass the case above and fail
    this one.

    The book is the cross-subset case above: BTC at cross 5x with a real
    maintenance gap of 1.625 against 1.600, and a large ETH leg at the isolated
    default. The one change is the venue holding 101 ETH where the ledger
    holds 100. ETH is entered and marked at 3000, so its uPnL is 0 on both
    sides and the equity and cash lines still agree. The size gap silences
    ETH's own exposure, and the maintenance alert still fires.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(
        store,
        equity="100000",
        leverage=LeverageBook(entries={"BTC": _CROSS_5X, "ETH": DEFAULT_LEVERAGE}),
        specs={"BTC": _BTC_SPEC, "ETH": _ETH_SPEC},
    )
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _book_fill(projection, quantity="100", price="3000", symbol="ETH")
    _mark(projection, "BTC", "65000")
    _mark(projection, "ETH", "3000")
    venue = _levered(
        _held(
            "100000.382",
            ("BTC", "0.002", "0.382"),
            ("ETH", "101", "0"),
            entry={"ETH": "3000"},
            free_margin="99974.382",
            maintenance="1.600",
        ),
        _CROSS_5X,
        symbol="BTC",
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [(d.tier, d.field, d.symbol) for d in divergences] == [
        (DivergenceTier.TIER_1, DivergenceField.SIGNED_SIZE, "ETH"),
        (DivergenceTier.TIER_2, DivergenceField.MAINTENANCE_MARGIN, None),
        (DivergenceTier.TIER_2, DivergenceField.NOTIONAL, "ETH"),
    ]
    assert _alerts(logs) == [
        {
            "field": "maintenance_margin",
            "symbol": None,
            "ledger": "1.625",
            "venue": "1.600",
        }
    ]
    record = _recorded(logs)
    assert (record["tier_1"], record["tier_2"], record["suppressed"]) == (1, 2, 0)


def test_a_missed_opening_fill_does_not_alert_its_own_unrealized_pnl_as_equity() -> None:
    """Rule (1) reaches a symbol the ledger is flat in (#322, review R001).

    The most common missed fill is an opening one. The ledger holds nothing,
    the venue holds 0.001 BTC entered at 55,000 and marked at 65,000, so its
    uPnL is 10 and the venue's equity is 10 over the ledger's. That gap is the
    missed fill in dollars. The size finding heals it this cycle, so an equity
    alert here is one an operator cannot act on.

    The ledger never traded BTC, so its Σ has no BTC term. The rule ranges
    over the Tier-1 symbols and not over what the ledger holds, because a size
    finding puts its symbol on at least one side by definition.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    _mark(keeper.portfolio, "BTC", "65000")
    venue = _held("100010", ("BTC", "0.001", "10"), entry={"BTC": "55000"})
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [(d.tier, d.field, d.symbol) for d in divergences] == [
        (DivergenceTier.TIER_1, DivergenceField.SIGNED_SIZE, "BTC"),
        (DivergenceTier.TIER_2, DivergenceField.EQUITY, None),
    ]
    assert _alerts(logs) == []
    record = _recorded(logs)
    assert (record["tier_1"], record["tier_2"], record["suppressed"]) == (1, 1, 0)


def test_a_cash_finding_the_mode_gate_refused_still_suppresses_the_account_grain() -> None:
    """The account half of ADR-0040 §6's first suppression, on the pass that
    makes the wording load-bearing (ADR-0046 §4).

    The rule reads the cycle's **findings**, never the heal it managed to book,
    and this is the pass where the two answers differ. A mid-run mode switch
    refuses the cash heal and freezes the account grain, while §4's amendment
    keeps *counting* the finding — so a suppression keyed on what was booked
    would fall silent on Tier-1 and shout on Tier-2, alerting an equity against
    the very snapshot the same pass has just declared unreliable. Under a pooled
    abstraction mode that snapshot reads an order of magnitude low, so the alert
    it would raise is guaranteed and meaningless.

    Scoped to the account grain, and it is scoped to **all three** of that
    grain's figures: the suppression is keyed on ``Divergence.symbol``, so
    ``maintenance_margin`` inherits it by carrying ``None`` rather than by being
    named, and that is the thing worth pinning. A maintenance figure whose
    comparison narrows to the cross subset (ADR-0046 §2.1) could plausibly have
    been recorded against the symbols it was summed over; recorded that way it
    would shout here, against the same snapshot the pass has just refused to
    heal from.

    So the run is at cross 5x with #152's tier-0 rate, which is what puts a
    maintenance finding on the grain at all — at the fixtures' default isolated
    1x the venue's cross-scoped field and our cross subset are both empty and
    agree at zero. The book: a 0.002 BTC long entered at 64809 and marked at
    65000 is worth 0.382 against a venue pricing it at 50.382, on a venue equity
    of 100051.382. The cash line that equity implies is ``100051.382 − 50.382 =
    100001`` against our 100000 — the Tier-1 finding — and equity (100000.382
    against 100051.382), free margin (99974.382 against 100001) and maintenance
    (``130.000 × 0.0125 = 1.6250000`` against 1.400) all disagree in its shadow.
    Every Tier-2 gap here is far outside its own band; only BTC's three
    per-symbol figures, which no Tier-1 finding explains, are alerted.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000", leverage=_BTC_CROSS_5X, specs={"BTC": _BTC_SPEC})
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _AccountVenue(
        _levered(_held("100051.382", ("BTC", "0.002", "50.382"), maintenance="1.400"), _CROSS_5X),
        mode=AccountModeVerdict.CHANGED,
    )
    cycle = LedgerReconciliation(exchange=venue, checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [(divergence.tier, divergence.field) for divergence in divergences] == [
        (DivergenceTier.TIER_1, DivergenceField.NET_CASH),
        (DivergenceTier.TIER_2, DivergenceField.EQUITY),
        (DivergenceTier.TIER_2, DivergenceField.FREE_MARGIN),
        (DivergenceTier.TIER_2, DivergenceField.MAINTENANCE_MARGIN),
        (DivergenceTier.TIER_2, DivergenceField.UNREALIZED_PNL),
        (DivergenceTier.TIER_2, DivergenceField.NOTIONAL),
        (DivergenceTier.TIER_2, DivergenceField.MARGIN_USED),
    ]
    assert [log["event"] for log in logs if log["event"] == NamedEvent.ACCOUNT_HEALED.value] == []
    assert _alerts(logs) == [
        {"field": "unrealized_pnl", "symbol": "BTC", "ledger": "0.382", "venue": "50.382"},
        {"field": "notional", "symbol": "BTC", "ledger": "130.000", "venue": "180.0000"},
        {"field": "margin_used", "symbol": "BTC", "ledger": "26.000", "venue": "36.0000"},
    ]


def test_a_stale_mark_suppresses_the_alert_and_is_counted_on_the_record() -> None:
    """ADR-0040 §6's second suppression, and the record that keeps it from being
    a silence.

    A divergence explained by staleness is not a bug: the figure is recomputed
    from ``(position, mark)`` on every read, so a mark that stopped arriving
    makes our side old rather than wrong, and the venue's instantaneous one is
    what it is measured against. Alerting there reports our own frozen input as
    the venue's disagreement.

    Suppressed and **counted**, which is the whole difference between this and
    doing nothing. ADR-0039 declined a max-age on the *read* path — a valuation
    is served at whatever mark is cached, and a reader has no clock to judge it
    with — and made this cycle the staleness safety net instead. A cycle that
    quietly dropped the finding would forfeit that job: an operator would read
    the same silence from a healthy book and from a mark stream that died an
    hour ago. ``suppressed`` on ``account.reconciled`` is the difference, beside
    ``unvalued`` and for the same reason.

    Both grains fall to one stale symbol, because both are Σs the stale term
    reaches: BTC's own three figures, and the equity computed from one of them.
    The book is
    behavior 1's — a 0.002 long entered at 64809 and marked at 65000 against a
    venue pricing it at 1.000 — which alerts on all four at a fresh mark;
    the only thing this case changes is a clock 90 seconds past the mark's
    stamp, against the 60-second horizon #142 measured skew over. Beyond it
    there is no evidence a band could be built from, so the figure is not banded
    wider, it is not alerted at all.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000", clock=ManualClock(_MARK_TS_NS + 90 * _NS_PER_SECOND))
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [divergence.field for divergence in divergences] == [
        DivergenceField.EQUITY,
        DivergenceField.UNREALIZED_PNL,
        DivergenceField.NOTIONAL,
        DivergenceField.MARGIN_USED,
    ]
    assert _alerts(logs) == []
    record = _recorded(logs)
    assert (record["tier_2"], record["suppressed"], record["unvalued"]) == (4, 4, 0)


def test_a_stale_cross_mark_suppresses_the_maintenance_finding() -> None:
    """A stale mark on a **cross** symbol suppresses the maintenance finding.

    Cross is the qualifier that matters. The figure is compared over the cross
    subset (ADR-0046 §2.1), so a stale term inside that subset makes it old,
    and a stale isolated mark does not (#305, pinned at the ``classify`` seam).
    This book holds one cross leg, so its stale mark is inside the subset.

    It needs a fixture of its own because the staleness family below cannot
    reach it. Those books configure no ``InstrumentSpec`` and sit at the
    isolated default, so ``_cross_maintenance`` never returns a number there and
    no maintenance finding is classified at all — the figure would be suppressed
    by not existing. This is behavior 8's cross book, which does produce one,
    with the only change a clock 90 seconds past the mark's stamp.

    So the pass classifies its 1.6250000 against 1.400 exactly as it did at a
    fresh mark, records it on ``tier_2``, and alerts nobody: our own frozen mark
    against the venue's instantaneous one makes the figure old rather than
    wrong. Counted on ``suppressed`` rather than dropped, which is the whole
    difference from silence — and ``unvalued`` stays 0, since every term was
    computable.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(
        store,
        equity="100000",
        leverage=_BTC_CROSS_5X,
        specs={"BTC": _BTC_SPEC},
        clock=ManualClock(_MARK_TS_NS + 90 * _NS_PER_SECOND),
    )
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _levered(
        _held(
            "100000.382",
            ("BTC", "0.002", "0.382"),
            free_margin="99974.382",
            maintenance="1.400",
        ),
        _CROSS_5X,
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [(d.field, d.symbol) for d in divergences] == [
        (DivergenceField.MAINTENANCE_MARGIN, None)
    ]
    assert _alerts(logs) == []
    record = _recorded(logs)
    assert (record["tier_2"], record["suppressed"], record["unvalued"]) == (1, 1, 0)


def test_an_absent_mark_alerts_nothing_and_lands_on_unvalued_rather_than_suppressed() -> None:
    """The other half of ADR-0040 §6's mark criterion, and the reason it is a
    second case rather than the stale one with a smaller number.

    A mark that never arrived and a mark that stopped arriving both end in
    silence, but they fail in different places. Absent fails at
    **classification**: ``_equity`` and ``_unrealized`` drop a figure they
    cannot compute, because a valuation waiting on a mark is unknown rather than
    divergent (ADR-0041 §6), so nothing ever reaches the band for the
    suppression above to act on. Stale fails at the **band**: the figure is
    real, computable and outside the tolerance, and this slice declines to alert
    on it.

    Which makes ``suppressed`` the assertion that carries the case. Read alone,
    ``_alerts(logs) == []`` here is vacuous — it would hold against a cycle that
    had no suppression rule at all — so what is pinned is that the two silences
    are **told apart on the record**: this pass reports ``unvalued=5,
    suppressed=0`` where behavior 7's identical book reports ``unvalued=0,
    suppressed=4``. An operator reading the cadence can therefore distinguish a
    book that was never valued from one whose valuation went stale, which is the
    whole of what ADR-0011 inv 1 asks of a count.

    **Five against four**, and the gap is the point rather than an untidiness:
    the two counts range over different sets, because a figure the band never
    sees is not a figure the count may skip. Stale reaches the band, so it is
    counted where the classification put it — four divergences, four
    suppressions, the symbol's uPnL, notional and posted margin beside the
    account's equity. Absent is dropped at classification, which is one figure
    further back: the same three per-symbol figures, and the account grain drops
    *both* of its own, equity and the free margin computed from it. Free margin
    is the whole of the difference — a figure that goes unknown without ever
    having been a divergence, so nothing was ever there to suppress.

    The per-symbol three is what makes ``unvalued`` a count of **figures** and
    not of symbols (ADR-0011 inv 1): one unmarked position now costs three,
    because its valuation, its exposure and the margin posted against it are
    three comparisons this pass did not make, and a book with three figures
    missing is not as unlooked-at as a book with six.

    Deliberately the same venue and the same fill as the stale case — a 0.002
    long entered at 64809 against a snapshot pricing it at 1.000 — so the only
    variable is the mark this case never feeds. Tier-1 stays silent on it: the
    snapshot's equity carries its own uPnL exactly, so ``venue_cash`` lands back
    on the 100000 cash line and the sizes agree, leaving the dropped Tier-2
    figures as the whole of what the pass had to say.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        assert asyncio.run(cycle.reconcile_account()) == ()

    assert _alerts(logs) == []
    record = _recorded(logs)
    assert (record["tier_2"], record["suppressed"], record["unvalued"]) == (0, 0, 5)


def test_a_stale_mark_for_a_symbol_traded_back_to_flat_silences_nothing() -> None:
    """The staleness range is **held** symbols, on the same predicate Tier-2's
    uPnL check reads — and this is the book that tells the two definitions apart.

    A closed position leaves its record behind at zero, and its mark keeps
    ageing after the exposure is gone. Read as held on *presence* rather than on
    net, that dead symbol goes stale on schedule and the account grain goes with
    it — ``equity`` and ``free_margin`` are Σs stale on their worst term — so a
    symbol the engine closed hours ago silences the book it is no longer part
    of. Every alert on it, indefinitely: nothing ever refreshes a mark for a
    symbol no strategy is trading.

    Two marks of different ages is what makes the case discriminating. ETH was
    bought and sold back to flat and its mark is 90 seconds old, past the
    60-second horizon; BTC is genuinely held and its mark was stamped at the
    clock this pass reads, so BTC alone can never explain a suppression. What is
    pinned is that every alert still fires and ``suppressed`` is zero — a pass
    that suppresses here is reporting a dead symbol's staleness as the live
    book's.

    The figures are the stale case's, deliberately: a 0.002 BTC long entered at
    64809 and marked at 65000 against a venue pricing it at 1.000. ETH closed at
    its entry, so it realizes nothing, values flat at a real zero and moves
    neither side's arithmetic — its whole contribution is a record with a stale
    mark on it.
    """
    store = SQLiteStore(":memory:")
    now_ns = _MARK_TS_NS + 90 * _NS_PER_SECOND
    keeper = _ledger(store, equity="100000", clock=ManualClock(now_ns))
    projection = keeper.portfolio
    _book_fill(projection, quantity="1", price="3000", symbol="ETH")
    _book_fill(projection, quantity="1", price="3000", symbol="ETH", side=Side.SELL, seq=2)
    _mark(projection, "ETH", "3000")
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000", ts_ns=now_ns)
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [divergence.field for divergence in divergences] == [
        DivergenceField.EQUITY,
        DivergenceField.UNREALIZED_PNL,
        DivergenceField.NOTIONAL,
        DivergenceField.MARGIN_USED,
    ]
    assert _alerts(logs) == [
        {"field": "equity", "symbol": None, "ledger": "100000.382", "venue": "100001.000"},
        {"field": "unrealized_pnl", "symbol": "BTC", "ledger": "0.382", "venue": "1.000"},
        {"field": "notional", "symbol": "BTC", "ledger": "130.000", "venue": "130.6180"},
        {"field": "margin_used", "symbol": "BTC", "ledger": "0.382", "venue": "1.000"},
    ]
    record = _recorded(logs)
    assert (record["tier_2"], record["suppressed"], record["unvalued"]) == (4, 0, 0)


def test_an_alerted_tier_2_divergence_still_changes_no_stored_value() -> None:
    """The no-write claim carried across the band, where it is newly losable.

    The pass above makes it against a gap the band absorbs, which is now the
    quiet half: a cycle that raises nothing has nothing it could be tempted to
    persist. This one makes it against a gap that **alerts** — the state where
    the cycle has produced an operator-facing finding and has, for the first
    time, a plausible reason to write: a last-alerted stamp, a de-duplication
    marker, or the venue's figure booked as the correction the record has just
    named. Any of those turns Tier-2 into a heal by the only measure ADR-0034
    draws the line on, and none would move a number a before/after comparison
    is watching — which is why the store is sealed rather than diffed.

    The alert count is asserted first and is load-bearing: without it the case
    passes on a cycle whose band absorbed everything, which is the neighbouring
    behavior and not this one. Behavior 1's book, for exactly that reason — a
    0.002 long entered at 64809 and marked at 65000 against a snapshot pricing
    it at 1.000, whose implied cash lands back on 100000 so both Tier-1 halves
    stay silent and the alerts are not suppressed by a finding that explains
    them.
    """
    store = _SealedStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _posted(keeper, ("BTC", "0.002", "0"))
    _mark(projection, "BTC", "65000")
    opened = store.load_account()
    stored = store.all_positions()
    assert opened is not None
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)
    store.seal()

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [alert["field"] for alert in _alerts(logs)] == [
        "equity",
        "unrealized_pnl",
        "notional",
        "margin_used",
    ]

    restored = store.load_account()
    assert restored is not None
    assert restored.cash == opened.cash == Decimal("100000")
    assert store.all_positions() == stored


def test_a_widened_band_is_the_one_the_cycle_judges_with() -> None:
    """The band is an **injected** tolerance, not a constant compiled into the
    classifier.

    An operator's reason to move it is the deployment's own: the term that does
    the work is relative to notional, so what counts as skew on a venue is a
    property of that venue's mark cadence and of the book's size, and a run that
    alerts every cycle is as useless as one that never does. What must not
    change with it is the classification — this pass finds the same four Tier-2
    figures a default band finds, and reports none of them.

    Behavior 1's book, moved only by the band: a 0.618 gap on a 130 notional is
    outside the default ``0.001`` relative term (0.13) and inside a ``0.01`` one
    (1.30). Asserting the divergences beside the silence is what keeps the case
    honest — a cycle that had simply stopped classifying would also raise no
    alert.
    """
    store = SQLiteStore(":memory:")
    keeper = _ledger(store, equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(
        exchange=_AccountVenue(venue),
        checkpointer=keeper,
        band=ValuationBand(rtol=Decimal("0.01")),
    )

    with capture_events() as logs:
        divergences = asyncio.run(cycle.reconcile_account())

    assert divergences is not None
    assert [divergence.field for divergence in divergences] == [
        DivergenceField.EQUITY,
        DivergenceField.UNREALIZED_PNL,
        DivergenceField.NOTIONAL,
        DivergenceField.MARGIN_USED,
    ]
    assert _alerts(logs) == []


def _during_the_wait(
    cycle: LedgerReconciliation,
    clock: ParkingClock,
    *acts: Callable[[], None],
    wait_for_marks: bool = True,
) -> tuple[Divergence, ...] | None:
    """Run one pass and do each of ``acts`` while the pass waits for a new mark.

    The loop runs between two acts, so the pass sees each one on its own. A
    pass that has ended skips the acts left, so the case fails on its
    assertions instead of hanging. After the last act the 5 s run out, since
    the pass checks every update until then (#690).
    """

    async def scenario() -> tuple[Divergence, ...] | None:
        task = asyncio.create_task(cycle.reconcile_account(wait_for_marks=wait_for_marks))
        parked = asyncio.create_task(clock.parked.wait())
        await asyncio.wait((task, parked), return_when=asyncio.FIRST_COMPLETED)
        parked.cancel()
        for act in acts:
            if task.done():
                break
            act()
            for _ in range(10):
                await asyncio.sleep(0)
        if not task.done():
            clock.advance_to(clock.timestamp_ns() + 5_000_000_000)
        return await task

    return asyncio.run(scenario())


def test_a_gap_the_next_mark_closes_is_not_alerted_and_is_counted_as_cleared() -> None:
    """The soak's one-step mark race (#682).

    The venue's read is about one mark update ahead of the feed. The ledger
    values BTC at 65000. The venue's 1.000 of uPnL on 0.002 BTC entered at
    64809 puts its mark at 65309. That gap is past the band on all four
    figures. The feed's next mark is 65309, and at that mark the ledger's
    uPnL is ``0.002 × 500 = 1.000``, the venue's own figure. So the pass
    waits, checks again, and finds nothing left to alert on.

    The race stays visible on the pass's record as ``cleared``.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle, clock, lambda: _mark(projection, "BTC", "65309", ts_ns=_MARK_TS_NS + 1)
        )

    assert _alerts(logs) == []
    assert _recorded(logs)["cleared"] == 4


def test_a_gap_the_next_mark_closes_on_a_short_is_not_alerted() -> None:
    """The same race on a short position (#695).

    The venue reports a short's size as negative and its notional as positive.
    So the clear rule must use the size without its sign, or no short could
    ever be at the venue's mark.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809", side=Side.SELL)
    _mark(projection, "BTC", "65000")
    venue = _held("99999.000", ("BTC", "-0.002", "-1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle, clock, lambda: _mark(projection, "BTC", "65309", ts_ns=_MARK_TS_NS + 1)
        )

    assert _alerts(logs) == []
    assert _recorded(logs)["cleared"] == 4


def test_a_gap_a_later_mark_closes_after_a_repeated_price_is_not_alerted() -> None:
    """The soak's race on a repeated price (#690).

    Hyperliquid pushes a mark about once a second but moves the price only
    about every 3 s. So the next mark often repeats the old price with a newer
    stamp. Here the feed pushes 65000 again, then the venue's 65309. The pass
    must keep checking until 65309 arrives, and then nothing is left to alert.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle,
            clock,
            lambda: _mark(projection, "BTC", "65000", ts_ns=_MARK_TS_NS + 1),
            lambda: _mark(projection, "BTC", "65309", ts_ns=_MARK_TS_NS + 2),
        )

    assert _alerts(logs) == []
    assert _recorded(logs)["cleared"] == 4


def test_a_gap_only_repeated_prices_follow_is_alerted_with_the_updates_it_checked() -> None:
    """Repeats of the old price never close a gap (#690).

    The feed pushes 65000 twice more with newer stamps, then the 5 s run out.
    Every alert stands. Each one names the mark of the last update checked,
    0.5 s old, and ``rechecked`` says two updates were checked. So "no new
    mark" and "two marks, none matched" read differently in the log.
    """
    clock = ParkingClock(start_ns=10_000_000_000)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000", ts_ns=9_000_000_000)
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle,
            clock,
            lambda: _mark(projection, "BTC", "65000", ts_ns=9_400_000_000),
            lambda: _mark(projection, "BTC", "65000", ts_ns=9_500_000_000),
        )

    alerts = [log for log in logs if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value]
    assert [(log["field"], log["marks"], log["rechecked"]) for log in alerts] == [
        ("equity", "BTC=65000 age=0.500s", 2),
        ("unrealized_pnl", "BTC=65000 age=0.500s", 2),
        ("notional", "BTC=65000 age=0.500s", 2),
        ("margin_used", "BTC=65000 age=0.500s", 2),
    ]
    assert _recorded(logs)["cleared"] == 0


def test_a_cost_basis_gap_a_mark_move_shifts_to_another_figure_is_still_alerted() -> None:
    """A real gap must not hide behind a price move (#690).

    The venue entered BTC at 64709, the ledger at 64809. The venue's cash is
    0.2 lower to match, so net cash agrees and Tier-1 finds nothing. Its free
    margin is pinned to the ledger's, so the case watches the mark-driven
    figures alone. At the venue's own mark of 65309, notional and equity
    agree. But ``unrealized_pnl`` is 1.000 against the venue's 1.200, a 0.2
    gap past the band.

    At 65409 the ledger's uPnL is 1.200 and that gap closes. Now notional and
    equity are 0.2 out instead. No update cleared every figure at once, so the
    pass alerts on the figures outside the band at the last update checked.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65309")
    venue = _held(
        "100001.000",
        ("BTC", "0.002", "1.200"),
        entry={"BTC": "64709"},
        free_margin="100000.000",
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle, clock, lambda: _mark(projection, "BTC", "65409", ts_ns=_MARK_TS_NS + 1)
        )

    assert _alerts(logs) == [
        {"field": "equity", "symbol": None, "ledger": "100001.200", "venue": "100001.000"},
        {"field": "notional", "symbol": "BTC", "ledger": "130.818", "venue": "130.618"},
    ]
    assert _recorded(logs)["cleared"] == 0


@pytest.mark.parametrize(
    ("venue_upnl", "venue_entry", "next_mark"),
    [
        pytest.param("1.200", "64709", "65359", id="mark above the venue's"),
        pytest.param("0.800", "64909", "65259", id="mark below the venue's"),
    ],
)
def test_a_cost_basis_gap_inside_the_band_at_a_mark_the_venue_never_had_is_still_alerted(
    venue_upnl: str, venue_entry: str, next_mark: str
) -> None:
    """Only the venue's own mark can clear an alert (#695).

    The same book as above. The real gap is 0.2 of uPnL, about 1.53 times the
    band. At 65359 the price move splits it: 0.1 shows in ``unrealized_pnl``
    and 0.1 in ``notional``. Each half is inside the band, so no figure is
    outside it. But the venue's mark is 65309, so this check proves nothing.
    The second case is the mirror. The gap has the other sign, and 65259, a
    mark below the venue's, splits it.

    No more marks arrive. Every alert stands with the first check's figures,
    the only ones outside the band. ``rechecked`` still counts the check.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65309")
    venue = _held(
        "100001.000",
        ("BTC", "0.002", venue_upnl),
        entry={"BTC": venue_entry},
        free_margin="100000.000",
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle, clock, lambda: _mark(projection, "BTC", next_mark, ts_ns=_MARK_TS_NS + 1)
        )

    assert _alerts(logs) == [
        {"field": "unrealized_pnl", "symbol": "BTC", "ledger": "1.000", "venue": venue_upnl},
        {"field": "margin_used", "symbol": "BTC", "ledger": "1.000", "venue": venue_upnl},
    ]
    alerts = [log for log in logs if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value]
    assert {log["rechecked"] for log in alerts} == {1}
    assert _recorded(logs)["cleared"] == 0


def test_an_in_band_check_at_a_mark_the_venue_never_had_does_not_end_the_wait() -> None:
    """The pass keeps checking until the venue's mark arrives (#695).

    The same book again. At 65359 the gap is split and in band. Then the feed
    sends 65309, the venue's own mark, and the 0.2 gap shows whole. The alerts
    carry that second check: its mark is 0.5 s old, where the first check's
    was 1 s old.
    """
    clock = ParkingClock(start_ns=10_000_000_000)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65309", ts_ns=9_000_000_000)
    venue = _held(
        "100001.000",
        ("BTC", "0.002", "1.200"),
        entry={"BTC": "64709"},
        free_margin="100000.000",
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle,
            clock,
            lambda: _mark(projection, "BTC", "65359", ts_ns=9_400_000_000),
            lambda: _mark(projection, "BTC", "65309", ts_ns=9_500_000_000),
        )

    alerts = [log for log in logs if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value]
    assert [(log["field"], log["ledger"], log["marks"], log["rechecked"]) for log in alerts] == [
        ("unrealized_pnl", "1.000", "BTC=65309 age=0.500s", 2),
        ("margin_used", "1.000", "BTC=65309 age=0.500s", 2),
    ]
    assert _recorded(logs)["cleared"] == 0


def test_alerts_carry_the_last_check_that_had_a_figure_outside_the_band() -> None:
    """An in-band check has no figure to log, so the one before it speaks (#695).

    The same book again. At 65325 the ledger's uPnL is ``0.002 × 516 = 1.032``
    against the venue's 1.200, still past the band. At 65359 the gap is split
    and in band, and no more marks arrive. The alerts carry the 65325 check,
    its figures and its mark together. ``rechecked`` counts both checks.
    """
    clock = ParkingClock(start_ns=10_000_000_000)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65309", ts_ns=9_000_000_000)
    venue = _held(
        "100001.000",
        ("BTC", "0.002", "1.200"),
        entry={"BTC": "64709"},
        free_margin="100000.000",
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle,
            clock,
            lambda: _mark(projection, "BTC", "65325", ts_ns=9_400_000_000),
            lambda: _mark(projection, "BTC", "65359", ts_ns=9_500_000_000),
        )

    alerts = [log for log in logs if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value]
    assert [(log["field"], log["ledger"], log["marks"], log["rechecked"]) for log in alerts] == [
        ("unrealized_pnl", "1.032", "BTC=65325 age=0.600s", 2),
        ("margin_used", "1.032", "BTC=65325 age=0.600s", 2),
    ]
    assert _recorded(logs)["cleared"] == 0


def test_a_cost_basis_gap_on_a_cross_book_is_still_alerted_off_the_venues_mark() -> None:
    """A cross book reaches the bug with no figure pinned (#695).

    The same BTC gap on cross 5x. Every venue figure is the one its own mark
    of 65309 gives. Its free margin is ``100001.000 − 130.618 / 5 =
    99974.8764`` and its maintenance is ``130.618 × 0.0125 = 1.632725``. So
    only ``unrealized_pnl`` is past the band. At 65359 no figure is.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(
        SQLiteStore(":memory:"),
        equity="100000",
        clock=clock,
        leverage=_BTC_CROSS_5X,
        specs={"BTC": _BTC_SPEC},
    )
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65309")
    venue = _levered(
        _held(
            "100001.000",
            ("BTC", "0.002", "1.200"),
            entry={"BTC": "64709"},
            free_margin="99974.8764",
            maintenance="1.632725",
        ),
        _CROSS_5X,
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle, clock, lambda: _mark(projection, "BTC", "65359", ts_ns=_MARK_TS_NS + 1)
        )

    assert _alerts(logs) == [
        {"field": "unrealized_pnl", "symbol": "BTC", "ledger": "1.000", "venue": "1.200"},
    ]
    assert _recorded(logs)["cleared"] == 0


def test_a_cost_basis_gap_beside_a_second_isolated_position_is_still_alerted() -> None:
    """The soak's own book, with BTC and ETH both open at isolated 1x (#695).

    The same BTC gap, with no figure pinned. ETH agrees with the venue: 0.1
    entered at 2990 and marked at 3000. The venue's free margin is
    ``100002.000 − 2.200 = 99999.800`` against the ledger's 100000.000. That
    0.2 gap would alert on BTC alone. Here the account's band is ``0.001 ×
    430.618``, so it is covered.

    The BTC alert then rests on figures a mark can move. At 65359 none of them
    is outside the band.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _book_fill(projection, quantity="0.1", price="2990", symbol="ETH")
    _mark(projection, "BTC", "65309")
    _mark(projection, "ETH", "3000")
    venue = _held(
        "100002.000",
        ("BTC", "0.002", "1.200"),
        ("ETH", "0.1", "1.000"),
        entry={"BTC": "64709", "ETH": "2990"},
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        divergences = _during_the_wait(
            cycle, clock, lambda: _mark(projection, "BTC", "65359", ts_ns=_MARK_TS_NS + 1)
        )

    assert divergences is not None
    assert [(d.field, d.ledger, d.venue) for d in divergences if d.symbol is None] == [
        (DivergenceField.FREE_MARGIN, Decimal("100000.000"), Decimal("99999.800"))
    ]
    assert _alerts(logs) == [
        {"field": "unrealized_pnl", "symbol": "BTC", "ledger": "1.000", "venue": "1.200"},
        {"field": "margin_used", "symbol": "BTC", "ledger": "1.000", "venue": "1.200"},
    ]
    assert _recorded(logs)["cleared"] == 0


def test_one_symbol_at_the_venues_mark_does_not_clear_a_gap_on_another() -> None:
    """Every symbol the alerts sum over must be at the venue's mark (#695).

    BTC is the race: 65000 against the venue's 65309. ETH is a real gap. The
    venue entered it at 2986, the ledger at 2990, so at the venue's mark of
    3000 the uPnL is 0.4 apart, past ETH's band of 0.3.

    Then BTC reaches 65309 exactly, and ETH moves to 3002. That splits ETH's
    gap into 0.2 of uPnL and 0.2 of notional, so no figure is outside the band.
    BTC is at the venue's mark but ETH is not. So every alert stands.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _book_fill(projection, quantity="0.1", price="2990", symbol="ETH")
    _mark(projection, "BTC", "65000")
    _mark(projection, "ETH", "3000")
    venue = _held(
        "100002.000",
        ("BTC", "0.002", "1.000"),
        ("ETH", "0.1", "1.400"),
        entry={"ETH": "2986"},
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    def both_marks() -> None:
        _mark(projection, "BTC", "65309", ts_ns=_MARK_TS_NS + 1)
        _mark(projection, "ETH", "3002", ts_ns=_MARK_TS_NS + 1)

    with capture_events() as logs:
        _during_the_wait(cycle, clock, both_marks)

    alerts = [log for log in logs if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value]
    assert {(log["field"], log["symbol"], Decimal(str(log["ledger"]))) for log in alerts} == {
        ("equity", None, Decimal("100001.382")),
        ("unrealized_pnl", "BTC", Decimal("0.382")),
        ("notional", "BTC", Decimal("130")),
        ("margin_used", "BTC", Decimal("0.382")),
        ("unrealized_pnl", "ETH", Decimal("1")),
        ("margin_used", "ETH", Decimal("1")),
    }
    assert {log["rechecked"] for log in alerts} == {1}
    assert _recorded(logs)["cleared"] == 0


def test_a_symbol_that_alerts_during_the_wait_must_reach_the_venues_mark_too() -> None:
    """A gap first seen at a later check cannot be cleared off the venue's mark (#695).

    ETH is the real gap of the case above, marked at 3002, where it is split
    and in band. BTC is the race, so only BTC alerts at the first check.

    Then ETH is at the venue's own 3000, and its 0.4 of uPnL shows whole. So
    ETH alerts too. Then BTC reaches 65309 and ETH is back at 3002. No figure
    is outside the band, but ETH is not at the venue's mark. So the alerts
    stand, with the figures of the check that showed ETH's gap.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _book_fill(projection, quantity="0.1", price="2990", symbol="ETH")
    _mark(projection, "BTC", "65160")
    _mark(projection, "ETH", "3002")
    venue = _held(
        "100002.000",
        ("BTC", "0.002", "1.000"),
        ("ETH", "0.1", "1.400"),
        entry={"ETH": "2986"},
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    def eth_at_the_venues_mark() -> None:
        _mark(projection, "ETH", "3000", ts_ns=_MARK_TS_NS + 1)
        _mark(projection, "BTC", "65160", ts_ns=_MARK_TS_NS + 1)

    def btc_at_the_venues_mark() -> None:
        _mark(projection, "ETH", "3002", ts_ns=_MARK_TS_NS + 2)
        _mark(projection, "BTC", "65309", ts_ns=_MARK_TS_NS + 2)

    with capture_events() as logs:
        _during_the_wait(cycle, clock, eth_at_the_venues_mark, btc_at_the_venues_mark)

    alerts = [log for log in logs if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value]
    assert {(log["field"], log["symbol"], Decimal(str(log["ledger"]))) for log in alerts} == {
        ("unrealized_pnl", "BTC", Decimal("0.702")),
        ("notional", "BTC", Decimal("130.32")),
        ("margin_used", "BTC", Decimal("0.702")),
        ("unrealized_pnl", "ETH", Decimal("1")),
        ("margin_used", "ETH", Decimal("1")),
    }
    assert {log["rechecked"] for log in alerts} == {2}
    assert _recorded(logs)["cleared"] == 0


def test_a_symbol_that_alerts_during_the_wait_joins_the_marks_waited_for() -> None:
    """A race that reaches a second symbol clears at both venue marks (#695).

    Both positions are sound, and only BTC alerts at the first check. Then ETH
    moves to 3004, 0.4 from the venue's and past its band of 0.3, so it alerts
    too. Then BTC reaches 65309, and after it ETH reaches 3000.

    The pass waits for a newer mark on both by then. So it checks once both are
    in, finds each at the venue's mark, and clears the three BTC alerts.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _book_fill(projection, quantity="0.1", price="2990", symbol="ETH")
    _mark(projection, "BTC", "65160")
    _mark(projection, "ETH", "3000")
    venue = _held(
        "100002.000",
        ("BTC", "0.002", "1.000"),
        ("ETH", "0.1", "1.000"),
        entry={"ETH": "2990"},
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    def eth_moves_away() -> None:
        _mark(projection, "ETH", "3004", ts_ns=_MARK_TS_NS + 1)
        _mark(projection, "BTC", "65160", ts_ns=_MARK_TS_NS + 1)

    with capture_events() as logs:
        _during_the_wait(
            cycle,
            clock,
            eth_moves_away,
            lambda: _mark(projection, "BTC", "65309", ts_ns=_MARK_TS_NS + 2),
            lambda: _mark(projection, "ETH", "3000", ts_ns=_MARK_TS_NS + 2),
        )

    assert _alerts(logs) == []
    assert _recorded(logs)["cleared"] == 3


def test_float_noise_in_the_venues_notional_does_not_keep_a_race_alerting() -> None:
    """The venue's notional can carry float noise (#695).

    Some large mainnet positions report a notional such as
    ``8584360.3213500008``. The plain race again, with noise of 5e-8 on the
    venue's 130.618. That is inside 1e-9 of the notional but over 1e-9 itself,
    so the tolerance must scale with the notional. The feed reaches 65309, so
    the alerts clear.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    (btc,) = venue.positions
    noisy = replace(venue, positions=(replace(btc, notional=Decimal("130.61800005")),))
    cycle = LedgerReconciliation(exchange=_AccountVenue(noisy), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle, clock, lambda: _mark(projection, "BTC", "65309", ts_ns=_MARK_TS_NS + 1)
        )

    assert _alerts(logs) == []
    assert _recorded(logs)["cleared"] == 4


def test_a_mark_close_to_the_venues_but_not_the_same_does_not_clear() -> None:
    """The tolerance absorbs float noise and nothing else (#695).

    The plain race again, but the feed stops at 65309.001. That is 1.5e-8 of
    the price away from the venue's 65309. No figure is outside the band
    there, but it is a different mark. So the alerts stand.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle, clock, lambda: _mark(projection, "BTC", "65309.001", ts_ns=_MARK_TS_NS + 1)
        )

    assert [(alert["field"], alert["symbol"]) for alert in _alerts(logs)] == [
        ("equity", None),
        ("unrealized_pnl", "BTC"),
        ("notional", "BTC"),
        ("margin_used", "BTC"),
    ]
    assert _recorded(logs)["cleared"] == 0


def test_a_pass_no_update_cleared_counts_nothing_as_cleared() -> None:
    """``cleared`` is 0 or every alert, never a part (#690).

    The same cost basis gap, first marked at 65000. Four figures alert. At
    65409 only notional and equity are still out. That update did not clear
    the account, so nothing was cleared, even though two figures closed.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held(
        "100001.000",
        ("BTC", "0.002", "1.200"),
        entry={"BTC": "64709"},
        free_margin="100000.000",
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle, clock, lambda: _mark(projection, "BTC", "65409", ts_ns=_MARK_TS_NS + 1)
        )

    assert [alert["field"] for alert in _alerts(logs)] == ["equity", "notional"]
    assert _recorded(logs)["cleared"] == 0


def test_a_gap_with_no_new_mark_says_it_checked_no_update() -> None:
    """``rechecked`` is 0 when the feed sends nothing in the wait (#690)."""
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(cycle, clock)

    alerts = [log for log in logs if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value]
    assert len(alerts) == 4
    assert {log["rechecked"] for log in alerts} == {0}


def test_an_alert_with_no_mark_to_wait_for_stands_at_once() -> None:
    """A maintenance alert computed from no symbol does not start the wait (#696).

    An operator opened a cross ETH position by hand, and an earlier pass healed
    it into the books. Config does not name ETH, so the ledger values it at the
    isolated 1x fallback, and the feed sends it no mark. The ledger's cross sum
    is then a sum over no symbols, which is 0. The venue publishes 2.500. No
    other figure can be compared, so this alert is the only one.

    No mark can change that 0. A wait here checked the same gap in a tight loop
    until the 5 s ran out. So the alert stands at once, and the clock must
    never see a sleep.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    _book_fill(keeper.portfolio, quantity="0.1", price="2990", symbol="ETH")
    venue = _levered(
        _held(
            "100001.000",
            ("ETH", "0.1", "1.000"),
            entry={"ETH": "2990"},
            maintenance="2.500",
        ),
        _CROSS_5X,
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(cycle, clock)

    alerts = [log for log in logs if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value]
    assert [(log["field"], log["venue"], log["rechecked"]) for log in alerts] == [
        ("maintenance_margin", "2.500", 0)
    ]
    assert _recorded(logs)["cleared"] == 0
    assert not clock.parked.is_set()


def test_an_alert_with_no_mark_to_wait_for_does_not_end_the_wait_of_one_beside_it() -> None:
    """A BTC alert keeps its wait beside an alert computed from no symbol (#696).

    The book is the one above, with the soak's BTC race beside it. The ledger
    values BTC at 65000 and the venue's mark is 65309. BTC's three figures have
    a mark to wait for, so the pass waits. At 65309 the ledger's notional is
    ``0.002 × 65309 = 130.618``, the venue's own figure.

    The ``maintenance_margin`` gap stays, so no check is clean and the pass
    must alert. ``rechecked`` is 1 because it checked the one new mark.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _book_fill(projection, quantity="0.1", price="2990", symbol="ETH")
    _mark(projection, "BTC", "65000")
    venue = _levered(
        _held(
            "100002.000",
            ("BTC", "0.002", "1.000"),
            ("ETH", "0.1", "1.000"),
            entry={"ETH": "2990"},
            maintenance="2.500",
        ),
        _CROSS_5X,
        symbol="ETH",
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle, clock, lambda: _mark(projection, "BTC", "65309", ts_ns=_MARK_TS_NS + 1)
        )

    alerts = [log for log in logs if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value]
    assert [(log["field"], log["venue"], log["rechecked"]) for log in alerts] == [
        ("maintenance_margin", "2.500", 1)
    ]
    assert clock.parked.is_set()


def test_an_alert_with_no_mark_to_wait_for_stands_on_the_pass_that_heals_its_gap() -> None:
    """The cost of not waiting: one more alert on the pass that heals (#696).

    BTC is configured cross 5x, and the ledger missed the fill that opened it.
    Net cash agrees, so only the size is healed. The ledger has no BTC row, so
    the size finding does not explain ``maintenance_margin`` (#322).

    The first pass confirms the size gap, and the second heals it. Both alert
    on ``maintenance_margin``, because the pass does not compare the healed
    ledger. The third pass does, and finds nothing.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(
        SQLiteStore(":memory:"),
        equity="100000",
        clock=clock,
        leverage=_BTC_CROSS_5X,
        specs={"BTC": _BTC_SPEC},
    )
    _mark(keeper.portfolio, "BTC", "65309")
    # 0.002 × 65309 = 130.618. A fifth is the margin, and 0.0125 is the rate.
    venue = _levered(
        _held(
            "100001.000",
            ("BTC", "0.002", "1.000"),
            free_margin="99974.8764",
            maintenance="1.632725",
        ),
        _CROSS_5X,
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    passes = []
    for _ in range(3):
        # Each pass runs on its own event loop, and an event belongs to one.
        clock.parked = asyncio.Event()
        with capture_events() as logs:
            _during_the_wait(cycle, clock)
        assert not clock.parked.is_set()
        passes.append(logs)

    alerts = [
        [log["field"] for log in logs if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value]
        for logs in passes
    ]
    assert alerts == [["maintenance_margin"], ["maintenance_margin"], []]
    assert [_recorded(logs)["unconfirmed"] for logs in passes] == [1, 0, 0]
    assert [_recorded(logs)["cleared"] for logs in passes] == [0, 0, 0]
    assert (_recorded(passes[2])["tier_1"], _recorded(passes[2])["tier_2"]) == (0, 0)


def test_a_pass_told_not_to_wait_alerts_on_its_first_check() -> None:
    """The pass flatten runs alerts at once, with ``rechecked`` 0 (#694).

    Same race as the case where the next mark closes the gap. A pass that
    waits clears all four alerts when 65309 arrives. This pass must not wait,
    so its alerts stand at 65000. At that mark the ledger's uPnL is
    ``0.002 × 191 = 0.382`` against the venue's 1.000. The clock must never
    see a sleep.
    """
    clock = ParkingClock(start_ns=10_000_000_000)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000", ts_ns=9_000_000_000)
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle,
            clock,
            lambda: _mark(projection, "BTC", "65309", ts_ns=9_500_000_000),
            wait_for_marks=False,
        )

    alerts = [log for log in logs if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value]
    assert [(log["field"], log["ledger"], log["marks"], log["rechecked"]) for log in alerts] == [
        ("equity", "100000.382", "BTC=65000 age=1.000s", 0),
        ("unrealized_pnl", "0.382", "BTC=65000 age=1.000s", 0),
        ("notional", "130.000", "BTC=65000 age=1.000s", 0),
        ("margin_used", "0.382", "BTC=65000 age=1.000s", 0),
    ]
    assert _recorded(logs)["cleared"] == 0
    assert not clock.parked.is_set()


def test_two_passes_told_not_to_wait_still_heal_a_size_gap() -> None:
    """Flatten runs these passes for their heals, so the heals must stay (#694).

    The venue holds 0.05 ETH the ledger has no row for. BTC is booked, but its
    venue uPnL is past the band, which is the alert a pass would wait on. A
    gap heals only on the second pass that finds it (#574). So the first pass
    books nothing and the second books the ETH.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    ledger = keeper.portfolio
    _book_fill(ledger, quantity="0.002", price="64809")
    _mark(ledger, "BTC", "65000")
    venue = _held(
        "100001.000", ("BTC", "0.002", "1.000"), ("ETH", "0.05", "0"), entry={"ETH": "3000"}
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as first:
        _during_the_wait(cycle, clock, wait_for_marks=False)

    assert not clock.parked.is_set()
    assert ledger.account_net() == {"BTC": Decimal("0.002")}
    assert _alerts(first) != []

    clock.advance_to(8)
    with capture_events() as second:
        _during_the_wait(cycle, clock, wait_for_marks=False)

    assert ledger.account_net() == {"BTC": Decimal("0.002"), "ETH": Decimal("0.05")}
    assert _alerts(second) != []
    healed = [
        [log["symbol"] for log in logs if log["event"] == NamedEvent.ACCOUNT_HEALED.value]
        for logs in (first, second)
    ]
    assert healed == [[], ["ETH"]]
    assert not clock.parked.is_set()


def test_a_gap_the_next_mark_does_not_close_is_alerted_at_that_mark() -> None:
    """A real gap survives the second check (#682).

    Same book and same venue as the race above, but the next mark is 65100,
    not the venue's 65309. At 65100 the ledger's uPnL is
    ``0.002 × 291 = 0.582`` against the venue's 1.000, still far past the band.
    So all four figures alert.

    The alert carries the ledger's figures at the new mark. That is the
    comparison the alert stands on, so it is the one an operator reads.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle, clock, lambda: _mark(projection, "BTC", "65100", ts_ns=_MARK_TS_NS + 1)
        )

    assert _alerts(logs) == [
        {"field": "equity", "symbol": None, "ledger": "100000.582", "venue": "100001.000"},
        {"field": "unrealized_pnl", "symbol": "BTC", "ledger": "0.582", "venue": "1.000"},
        {"field": "notional", "symbol": "BTC", "ledger": "130.200", "venue": "130.6180"},
        {"field": "margin_used", "symbol": "BTC", "ledger": "0.582", "venue": "1.000"},
    ]
    assert _recorded(logs)["cleared"] == 0


def test_a_gap_with_no_new_mark_inside_the_wait_is_still_alerted() -> None:
    """A missing mark never hides a gap (#682).

    The pass waits at most 5 s for the next mark. Here the feed sends nothing
    and the clock moves 5 s on. So the pass alerts on the comparison it has,
    the ledger at 65000 against the venue at 65309, as it did before the
    second check existed.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle, clock, lambda: clock.advance_to(clock.timestamp_ns() + 5_000_000_000)
        )

    assert _alerts(logs) == [
        {"field": "equity", "symbol": None, "ledger": "100000.382", "venue": "100001.000"},
        {"field": "unrealized_pnl", "symbol": "BTC", "ledger": "0.382", "venue": "1.000"},
        {"field": "notional", "symbol": "BTC", "ledger": "130.000", "venue": "130.6180"},
        {"field": "margin_used", "symbol": "BTC", "ledger": "0.382", "venue": "1.000"},
    ]
    assert _recorded(logs)["cleared"] == 0


def test_an_equity_gap_waits_for_a_new_mark_on_every_held_symbol() -> None:
    """Equity is ``cash + Σ uPnL``, so its second check needs every term fresh (#682).

    The book holds BTC and ETH. ETH agrees with the venue: 0.1 entered at 2990
    and marked at 3000 is 1.000 of uPnL on both sides. BTC is the same race as
    above: 65000 against the venue's 65309. So the equity gap is BTC's alone,
    and the next BTC mark at 65309 closes it.

    But ETH sends no new mark before the 5 s are up. A second check that
    valued equity off a new BTC mark and an old ETH mark would mix two moments.
    So the equity alert stands.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _book_fill(projection, quantity="0.1", price="2990", symbol="ETH")
    _mark(projection, "BTC", "65000")
    _mark(projection, "ETH", "3000")
    venue = _held(
        "100002.000",
        ("BTC", "0.002", "1.000"),
        ("ETH", "0.1", "1.000"),
        entry={"ETH": "2990"},
    )
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle,
            clock,
            lambda: _mark(projection, "BTC", "65309", ts_ns=_MARK_TS_NS + 1),
            lambda: clock.advance_to(clock.timestamp_ns() + 5_000_000_000),
        )

    assert {"field": "equity", "symbol": None, "ledger": "100001.382", "venue": "100002.000"} in (
        _alerts(logs)
    )


def test_a_fill_during_the_wait_leaves_the_first_checks_alerts_standing() -> None:
    """A fill inside the wait must not clear a real gap (#682).

    The gap is real: the next mark is 65100, not the venue's 65309. But a
    second 0.002 BTC fill lands while the pass waits. The venue snapshot was
    read before it, so a second check would find a BTC size gap at Tier-1.
    A Tier-1 finding silences that symbol's Tier-2 figures, and the real gap
    would vanish from the alerts. So the pass skips the second check and
    alerts on the first one, as it did before the second check existed.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle,
            clock,
            lambda: _book_fill(projection, quantity="0.002", price="65050", seq=2),
            lambda: _mark(projection, "BTC", "65100", ts_ns=_MARK_TS_NS + 1),
        )

    assert _alerts(logs) == [
        {"field": "equity", "symbol": None, "ledger": "100000.382", "venue": "100001.000"},
        {"field": "unrealized_pnl", "symbol": "BTC", "ledger": "0.382", "venue": "1.000"},
        {"field": "notional", "symbol": "BTC", "ledger": "130.000", "venue": "130.6180"},
        {"field": "margin_used", "symbol": "BTC", "ledger": "0.382", "venue": "1.000"},
    ]
    assert _recorded(logs)["cleared"] == 0


def test_a_fill_after_a_repeated_price_ends_the_wait_with_every_alert_standing() -> None:
    """A fill still ends the wait after an update was checked (#690).

    The feed repeats 65000 and the pass checks it. Then a 0.002 BTC fill
    lands, and then the venue's 65309. At 65309 the fill's size gap would
    silence BTC's figures and the pass would look clear. So the fill ends the
    wait. The alerts stand at the last update checked, the repeat, and
    ``rechecked`` says one update was checked.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle,
            clock,
            lambda: _mark(projection, "BTC", "65000", ts_ns=_MARK_TS_NS + 1),
            lambda: _book_fill(projection, quantity="0.002", price="65050", seq=2),
            lambda: _mark(projection, "BTC", "65309", ts_ns=_MARK_TS_NS + 2),
        )

    alerts = [log for log in logs if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value]
    assert [(log["field"], log["ledger"], log["rechecked"]) for log in alerts] == [
        ("equity", "100000.382", 1),
        ("unrealized_pnl", "0.382", 1),
        ("notional", "130.000", 1),
        ("margin_used", "0.382", 1),
    ]
    assert _recorded(logs)["cleared"] == 0


def test_a_funding_payment_during_the_wait_leaves_the_first_checks_alerts_standing() -> None:
    """A funding payment inside the wait must not clear a real gap (#682).

    The gap is real: the next mark is 65100, not the venue's 65309. But a
    funding payment lands while the pass waits. The venue snapshot was read
    before it, so a second check would find a cash gap at Tier-1. A cash
    finding silences every account figure, and the real equity gap would
    vanish from the alerts. Funding settles on the hour, which is when the
    soak saw most of these gaps.
    """
    clock = ParkingClock(start_ns=7)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)
    funding = FundingAccrual(
        ts_event=3_600,
        ts_init=3_600,
        account_id=LIVE_ACCOUNT_ID,
        symbol="BTC",
        boundary_ts_ns=3_600,
        amount=Decimal("-0.002762"),
    )

    with capture_events() as logs:
        _during_the_wait(
            cycle,
            clock,
            lambda: keeper.checkpoint_funding(funding),
            lambda: _mark(projection, "BTC", "65100", ts_ns=_MARK_TS_NS + 1),
        )

    assert _alerts(logs) == [
        {"field": "equity", "symbol": None, "ledger": "100000.382", "venue": "100001.000"},
        {"field": "unrealized_pnl", "symbol": "BTC", "ledger": "0.382", "venue": "1.000"},
        {"field": "notional", "symbol": "BTC", "ledger": "130.000", "venue": "130.6180"},
        {"field": "margin_used", "symbol": "BTC", "ledger": "0.382", "venue": "1.000"},
    ]
    assert _recorded(logs)["cleared"] == 0


def test_an_alert_names_the_mark_its_ledger_figure_was_valued_at_and_its_age() -> None:
    """The next unexplained gap can be read from the log alone (#682).

    The soak had to line every alert up against the Kafka topic to learn which
    mark the ledger used. So the alert now carries it, for every symbol the
    figure sums over. Here the second check values BTC at 65100, a mark
    stamped 0.3 s before the clock reads it. Equity sums BTC alone, so it
    names the same one mark.
    """
    clock = ParkingClock(start_ns=10_000_000_000)
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000", clock=clock)
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000", ts_ns=9_000_000_000)
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        _during_the_wait(
            cycle, clock, lambda: _mark(projection, "BTC", "65100", ts_ns=9_700_000_000)
        )

    marks = {
        log["field"]: log["marks"]
        for log in logs
        if log["event"] == NamedEvent.VALUATION_DIVERGENCE.value
    }
    named = "BTC=65100 age=0.300s"
    assert marks == {
        "equity": named,
        "unrealized_pnl": named,
        "notional": named,
        "margin_used": named,
    }


def test_an_alert_drops_the_ledger_figures_trailing_zeros_and_no_other_digit() -> None:
    """The soak logged a ledger equity of ``281.7231110000000000000000000`` (#682).

    The trailing zeros come from the ledger's exact arithmetic and say
    nothing, so the log drops them. It drops nothing else, so the figure shown
    is the figure compared, even where it has more decimals than the venue's.

    Here the mark is ``65000.0004`` with a long tail of zeros. So the uPnL is
    ``0.002 × 191.0004 = 0.3820008`` followed by the same zeros, and the
    notional is ``0.002 × 65000.0004 = 130.0000008``.
    """
    keeper = _ledger(SQLiteStore(":memory:"), equity="100000")
    projection = keeper.portfolio
    _book_fill(projection, quantity="0.002", price="64809")
    _mark(projection, "BTC", "65000.00040000000000000000")
    venue = _held("100001.000", ("BTC", "0.002", "1.000"))
    cycle = LedgerReconciliation(exchange=_AccountVenue(venue), checkpointer=keeper)

    with capture_events() as logs:
        asyncio.run(cycle.reconcile_account())

    assert _alerts(logs) == [
        {"field": "equity", "symbol": None, "ledger": "100000.3820008", "venue": "100001.000"},
        {"field": "unrealized_pnl", "symbol": "BTC", "ledger": "0.3820008", "venue": "1.000"},
        {"field": "notional", "symbol": "BTC", "ledger": "130.0000008", "venue": "130.6180"},
        {"field": "margin_used", "symbol": "BTC", "ledger": "0.3820008", "venue": "1.000"},
    ]
