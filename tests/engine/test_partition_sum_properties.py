"""Property suite: the Σ-invariant's engine-side half (ADR-0034/0041 §4).

ADR-0034 states it in three terms — ``Σ(per-strategy signed size) = account net
= venue szi``. **This suite is about the first equality only.** The ``= venue
szi`` half has no paper counterpart to check against: the in-process venue holds
no position state of its own, so there is nothing to disagree with, and that
half arrives with the live reconcile cadence. Read this as pinning that the
account grain is *constructed* from the partitions without loss — never as
evidence that the book matches a venue.

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

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from ledgers import GENESIS

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AccountSpec,
    Order,
    OrderState,
    OrderType,
    Position,
    Side,
)
from tickwright.engine.checkpoint import Checkpointer

_SPEC = AccountSpec(account_id="paper-default", genesis_collateral=GENESIS)

_PRICE = Decimal("100")
"""Fixed, so a divergence can only come from the sizes. The property is about
attribution, never about valuation — ``tests/domain/test_valuation.py`` owns
that."""

_OWNERSHIP = {"alpha": "BTC", "beta": "ETH", "gamma": "SOL"}
"""One symbol per strategy — ADR-0034's disjointness rule, which
``StrategyHost.register`` now enforces. Modelling overlapping ownership here
would make the suite assert over a book the engine refuses to assemble."""

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

    partitions: dict[str, Decimal] = {}
    for owner in (*sorted(_OWNERSHIP), None):
        for view in portfolio.open_positions(strategy_id=owner):
            partitions[view.symbol] = partitions.get(view.symbol, Decimal(0)) + view.size
    assert _nonzero(partitions) == _nonzero(expected)
