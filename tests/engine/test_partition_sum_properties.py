"""Property suite: the Σ-invariant's engine-side half (ADR-0034/0041 §4).

ADR-0034 states it in three terms — ``Σ(per-strategy signed size) = account net
= venue szi`` — and the two properties here take one equality each.

The **first** is checked on a paper-shaped ledger, where it is the only one
available: the in-process venue holds no position state of its own, so there is
nothing to disagree with. Read that one as pinning that the account grain is
*constructed* from the partitions without loss — never as evidence that the book
matches a venue.

The **second** is the ``= venue szi`` half, which only exists on the live
reconcile cadence, since that is where a second account truth exists at all. It
is not the first restated against a different oracle: the first says the ledger
is internally consistent however it got there, and this one says a *healed*
ledger has been made to agree with a book it did not produce.

The unattributed partition is in scope and is the reason the property is worth
driving: it is the one partition no strategy can reach, so a Σ that quietly
skipped it would still satisfy every per-strategy read (see
``tests/engine/test_portfolio.py``) while netting to something no venue reports.

The oracle is the **signed sum of the fills the test fed**, kept by the test in
a plain dict. That is deliberately not how the projection gets its answer: it
folds each fill through ``Position.apply``'s average-cost accounting, with
flip-through-zero producing two changes and an idempotency set behind it, then
aggregates the partitions. Re-folding the projection's own partitions here would
be green by construction and blind to the whole fold going wrong.

Everything is real: ``SQLiteStore(":memory:")``, the ``Checkpointer``, the
``PortfolioProjection``.
"""

import asyncio
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from ledgers import GENESIS
from venue_doubles import LIVE_ACCOUNT_ID, LiveVenueDouble, account_state

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AccountSpec,
    Order,
    OrderState,
    OrderType,
    PlaceOrder,
    Position,
    Side,
    VenueAccountState,
    VenueOrderView,
    VenuePositionState,
    VenueReadFailure,
)
from tickwright.engine.checkpoint import Checkpointer
from tickwright.engine.ledger_reconcile import LedgerReconciliation
from tickwright.engine.portfolio import PortfolioProjection

_SPEC = AccountSpec(account_id="paper-default", genesis_collateral=GENESIS)

_PRICE = Decimal("100")
"""Fixed, so a divergence can only come from the sizes. The property is about
attribution, never about valuation — ``tests/domain/test_valuation.py`` owns
that."""

_OWNERSHIP = {"alpha": "BTC", "beta": "ETH", "gamma": "SOL"}
"""One symbol per strategy — ADR-0034's disjointness rule, which
``StrategyHost.register`` now enforces. Modelling overlapping ownership here
would make the suite assert over a book the engine refuses to assemble."""

_UNTRADED_SYMBOL = "DOGE"
"""A symbol no strategy owns, so the venue reporting it is flow the engine never
placed — the case the unattributed partition exists for, and the one a Σ taken
over the strategies alone cannot see."""

_FOREIGN_SYMBOL = "BTC"
"""The unattributed partition shares a symbol with a strategy on purpose: a Σ
that dropped it would still agree with the account net on every *other* symbol,
so a symbol with only foreign flow would not catch the bug this suite exists
for."""


def _checkpointer(store: SQLiteStore) -> Checkpointer:
    """A fresh process's worth of state over an existing store.

    Called again mid-stream to model a restart: every partition it holds is
    rebuilt from ``all_positions``, so a restart is also what brings the
    unattributed row — written below the engine, reachable through no write verb
    — into memory at all.
    """
    checkpointer = Checkpointer(spec=_SPEC, store=store, clock=ManualClock(start_ns=1_000))
    checkpointer.recover()
    return checkpointer


def _file_foreign_flow(store: SQLiteStore, *, signed_size: Decimal) -> None:
    """Put a partition the engine never placed into the durable record.

    Written straight at the ``Store`` seam because that is the only way it can
    be: ``OrderFillEvent.strategy_id`` is ``str``, so no engine write verb can
    fold an unattributed fill. On live it is the reconciler's heal that leaves
    this row; what matters to this property is only that it is *there* and that
    recovery restores it unfiltered (ADR-0043 §9).
    """
    account = store.load_account()
    assert account is not None
    store.checkpoint_ledger(
        account=account,
        positions=(
            Position(
                strategy_id=None,
                symbol=_FOREIGN_SYMBOL,
                signed_size=signed_size,
                entry_price=_PRICE,
            ),
        ),
        ts_ns=1_000,
    )


def _apply(
    checkpointer: Checkpointer, *, seq: int, strategy_id: str, buy: bool, qty: Decimal
) -> None:
    """One fill, through the engine's real write path.

    One fill per order, which is what makes an arbitrary delivery order legal to
    generate: two fills of *one* order carry a monotonic ``cum_qty`` and could
    not be interleaved freely.
    """
    symbol = _OWNERSHIP[strategy_id]
    side = Side.BUY if buy else Side.SELL
    order = Order(
        cloid=f"0x{strategy_id}-{seq}",
        strategy_id=strategy_id,
        signal_id=f"{strategy_id}:{symbol}:{seq}",
        symbol=symbol,
        side=side,
        quantity=qty,
        order_type=OrderType.MARKET,
        state=OrderState.SUBMITTED,
    )
    event = order.record_fill(
        trade_id=f"{strategy_id}-{seq}",
        quantity=qty,
        price=_PRICE,
        ts_event=1_000 + seq,
        ts_init=1_000 + seq,
    )
    assert event is not None
    checkpointer.checkpoint_fill(order, event, side=side)


def _nonzero(net: dict[str, Decimal]) -> dict[str, Decimal]:
    """``account_net_size`` keeps a traded-to-flat symbol at zero, which is a
    real answer rather than an absence; the oracle has no reason to carry the
    key. Normalised on both sides so the comparison is about the numbers."""
    return {symbol: size for symbol, size in net.items() if size != 0}


@settings(deadline=None)
@given(
    fills=st.lists(
        st.tuples(
            st.sampled_from(sorted(_OWNERSHIP)),
            st.booleans(),
            st.decimals(min_value=Decimal("0.001"), max_value=Decimal("50"), places=3),
        ),
        min_size=1,
        max_size=12,
    ),
    foreign=st.decimals(min_value=Decimal("-20"), max_value=Decimal("20"), places=3),
    restart_after=st.integers(min_value=0, max_value=12),
)
def test_every_partition_sums_to_the_account_net_across_a_restart(
    fills: list[tuple[str, bool, Decimal]], foreign: Decimal, restart_after: int
) -> None:
    """Σ over every partition equals the account net, per symbol.

    Driven under an arbitrary interleaving of fills across strategies and sides,
    with a restart at an arbitrary point in the stream and an unattributed
    partition present throughout. Three quantities must agree, and each is
    reached a different way: the test's own signed sum of what it fed; the
    aggregation the reconciler reads (``account_net``); and the Σ a reader
    assembles from the per-partition read surface, which is what a caller
    holding only views can see.

    The restart matters because the two sides of the equality survive it
    differently: the aggregation is recomputed from partitions rebuilt out of
    the store, so a restart that dropped, merged or mis-keyed a partition would
    break the identity here and nowhere in the per-strategy reads.
    """
    store = SQLiteStore(":memory:")
    checkpointer = _checkpointer(store)
    _file_foreign_flow(store, signed_size=foreign)

    expected: dict[str, Decimal] = {_FOREIGN_SYMBOL: foreign}
    for seq, (strategy_id, buy, qty) in enumerate(fills, start=1):
        if seq == restart_after:
            checkpointer = _checkpointer(store)  # a new process, an empty memory
        _apply(checkpointer, seq=seq, strategy_id=strategy_id, buy=buy, qty=qty)
        symbol = _OWNERSHIP[strategy_id]
        expected[symbol] = expected.get(symbol, Decimal(0)) + (qty if buy else -qty)

    # Always end on a fresh process, so the assertions below read a book
    # rebuilt from the durable record rather than the one just folded in memory.
    portfolio = _checkpointer(store).portfolio

    assert _nonzero(portfolio.account_net()) == _nonzero(expected)

    assert _nonzero(_partition_sums(portfolio)) == _nonzero(expected)


class _AccountVenue(LiveVenueDouble):
    """A live venue answering one account snapshot, and nothing else.

    The three members the base withholds carry each double's meaning, and this
    one's is that the account cycle is anchored on the snapshot: an order action
    or a cloid read reaching the seam is the property being driven through a
    path it makes no claim about.
    """

    async def place(self, order: PlaceOrder) -> None:
        raise AssertionError("the account cycle places nothing")

    async def cancel(self, cloid: str) -> None:
        raise AssertionError("the account cycle cancels nothing")

    async def fetch_order(self, cloid: str) -> VenueOrderView | VenueReadFailure:
        raise AssertionError("the account cycle is anchored on the account snapshot, not a cloid")


_LIVE_SPEC = AccountSpec(account_id=LIVE_ACCOUNT_ID, genesis_collateral=None)
"""Live-shaped — an *ingested* genesis (ADR-0042 §6) — because the reconcile
cadence is live-only: paper has no second account for a cycle to compare
against, so a paper ledger could not reach the heal this property is about."""

_EQUITY = "100000"
"""The account the cycle opens against, and the equity every snapshot below
reports. Held constant so the cash line neither diverges nor heals: this
property is about sizes, and a cash correction moving underneath it would be a
second reason for the book to change."""


def _venue_state(sizes: dict[str, Decimal]) -> VenueAccountState:
    """The venue's own book: one position per symbol, priced at ``_PRICE``.

    Priced at the *fills'* price on purpose. A heal booked at a different one
    would realize PnL as it closed through a partition, which is real behavior
    (``test_ledger_reconcile``) and pure noise here — the sizes are the claim,
    so nothing else is allowed to move.
    """
    return VenueAccountState(
        equity=Decimal(_EQUITY),
        free_margin=Decimal("0.0096"),
        cross_maintenance_margin=Decimal("1.6198"),
        positions=tuple(
            VenuePositionState(
                symbol=symbol,
                signed_size=size,
                entry_price=_PRICE,
                notional=abs(size) * _PRICE,
                unrealized_pnl=Decimal(0),
                margin_used=Decimal(0),
                isolated_collateral=None,
                liquidation_price=None,
            )
            for symbol, size in sorted(sizes.items())
        ),
    )


def _partition_sums(portfolio: PortfolioProjection) -> dict[str, Decimal]:
    """Σ signed size per symbol over **every** partition, the unattributed one
    included — what a reader holding only the per-partition views can see."""
    sums: dict[str, Decimal] = {}
    for owner in (*sorted(_OWNERSHIP), None):
        for view in portfolio.open_positions(strategy_id=owner):
            sums[view.symbol] = sums.get(view.symbol, Decimal(0)) + view.size
    return sums


@settings(deadline=None)
@given(
    fills=st.lists(
        st.tuples(
            st.sampled_from(sorted(_OWNERSHIP)),
            st.booleans(),
            st.decimals(min_value=Decimal("0.001"), max_value=Decimal("50"), places=3),
        ),
        max_size=8,
    ),
    venue=st.dictionaries(
        st.sampled_from([*sorted(_OWNERSHIP.values()), _UNTRADED_SYMBOL]),
        st.tuples(
            st.booleans(),
            st.decimals(min_value=Decimal("0.001"), max_value=Decimal("20"), places=3),
        ).map(lambda signed: signed[1] if signed[0] else -signed[1]),
        min_size=1,
        max_size=4,
    ),
)
def test_a_healed_book_sums_to_the_venues_own_size_on_every_symbol_it_reports(
    fills: list[tuple[str, bool, Decimal]], venue: dict[str, Decimal]
) -> None:
    """After one reconcile cycle, Σ over every partition equals the venue's
    ``szi``, per symbol — ADR-0034's second equality.

    The oracle is the snapshot the venue was told to answer, which is the whole
    point of driving it this way: it is a book the engine did not produce and
    cannot derive, so an implementation that healed by recomputing its own net
    would satisfy the first property and fail this one.

    Driven over an arbitrary interleaving of real fills and an arbitrary venue
    book, so the cases fall out rather than being enumerated: a symbol both
    sides hold at different sizes, one the venue holds and no strategy ever
    traded (foreign flow), and one whose gap is a close rather than an open —
    the heal absorbs all three into the unattributed partition, and the Σ is
    what has to hold across them.

    Scoped to **the symbols the venue reports**, which is the honest statement of
    the invariant rather than a convenience: a symbol the ledger holds and the
    venue has closed comes back with no entry price, and ADR-0034's synthetic
    needs one to book against, so that finding is reported and left unhealed
    (pinned in ``test_ledger_reconcile``). Asserting over the union here would
    be asserting a heal the cycle deliberately declines to attempt.
    """
    store = SQLiteStore(":memory:")
    keeper = Checkpointer(spec=_LIVE_SPEC, store=store, clock=ManualClock(start_ns=1_000))
    keeper.recover()
    keeper.portfolio.materialise(account_state(_EQUITY))
    for seq, (strategy_id, buy, qty) in enumerate(fills, start=1):
        _apply(keeper, seq=seq, strategy_id=strategy_id, buy=buy, qty=qty)

    cycle = LedgerReconciliation(
        exchange=_AccountVenue(state=_venue_state(venue)), checkpointer=keeper
    )
    asyncio.run(cycle.reconcile_account())

    portfolio = keeper.portfolio
    sums = _partition_sums(portfolio)
    net = portfolio.account_net()
    for symbol, size in venue.items():
        assert sums.get(symbol, Decimal(0)) == size
        assert net.get(symbol, Decimal(0)) == size
