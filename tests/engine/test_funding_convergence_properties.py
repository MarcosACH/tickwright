"""Property suite: funding converges on one payment per boundary (ADR-0037).

The three convergence paths ADR-0037 names all reduce to the same shape — a
boundary already applied being offered again — and only their *reason* differs:
paper's catch-up re-fires within a run, live's reconnect re-reads history across
one, and a replay rerun re-derives every boundary in the file. Hypothesis drives
that shape directly: an ascending boundary stream, redelivered arbitrarily, with
a **restart** anywhere in it — a genuinely new ``Checkpointer`` over the same
store, which is what empties the in-memory state and leaves only the durable
watermark standing between a re-delivery and a double-count (ADR-0043 §5.2).

Everything is real: ``SQLiteStore(":memory:")``, the ``Checkpointer``, the
``PortfolioProjection``. The oracle is a **count**, not a re-derivation — the
ledger should hold one payment per distinct boundary, whatever the delivery
pattern was — so it cannot agree with the code by construction.
"""

from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from ledgers import GENESIS

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    AccountSpec,
    FundingAccrual,
    Order,
    OrderState,
    OrderType,
    Side,
)
from tickwright.engine.checkpoint import Checkpointer

_HOUR = 3_600_000_000_000
_SPEC = AccountSpec(account_id="paper-default", genesis_collateral=GENESIS)
_AMOUNT = Decimal("-2.5")
"""One boundary's payment. Fixed, so the oracle can be ``count x amount`` — the
property is about *how many times* a payment lands, never about its arithmetic,
which ``tests/domain/test_economics.py`` owns."""


def _checkpointer(store: SQLiteStore) -> Checkpointer:
    """A fresh process's worth of state over an existing store.

    Called again mid-stream to model a restart: the projection, its partitions
    and every in-memory key it holds are new, and the store is all that carries
    over — which is exactly the condition under which an in-memory dedup set
    would let a re-delivery through.
    """
    checkpointer = Checkpointer(spec=_SPEC, store=store, clock=ManualClock(start_ns=1_000))
    checkpointer.recover()
    return checkpointer


def _open_a_position(checkpointer: Checkpointer) -> None:
    """One filled BUY, so there is a partition for the accruals to land on."""
    order = Order(
        cloid="0xtrivial-1",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("0.5"),
        order_type=OrderType.MARKET,
        state=OrderState.SUBMITTED,
    )
    event = order.record_fill(
        trade_id="f1",
        quantity=Decimal("0.5"),
        price=Decimal("42000"),
        ts_event=1_000,
        ts_init=1_000,
    )
    assert event is not None
    checkpointer.checkpoint_fill(order, event, side=order.side)


def _accrual(boundary: int) -> FundingAccrual:
    return FundingAccrual(
        ts_event=boundary * _HOUR,
        ts_init=boundary * _HOUR,
        account_id=_SPEC.account_id,
        symbol="BTC",
        boundary_ts_ns=boundary * _HOUR,
        amount=_AMOUNT,
    )


@settings(deadline=None)
@given(
    boundaries=st.lists(
        st.integers(min_value=1, max_value=12), min_size=1, max_size=6, unique=True
    ),
    passes=st.integers(min_value=1, max_value=4),
    restart_before=st.integers(min_value=0, max_value=4),
)
def test_redelivery_across_a_restart_converges_on_one_payment_per_boundary(
    boundaries: list[int], passes: int, restart_before: int
) -> None:
    """However many times the stream is replayed, the ledger holds each once.

    Each pass delivers the whole ascending boundary set — the shape of a
    reconnect's history read and of a replay rerun alike — and one of those
    passes is preceded by a restart, so at least one re-delivery meets a ledger
    with no in-memory memory of having applied it. Ascending because that is
    what the delivery contract guarantees (the adapter sorts each batch before
    applying); the watermark is only ever as safe as the ordering it is fed.

    Both halves of the write are asserted, because they move under one
    transaction and a gate that leaked on either would be a real defect: the
    funding line the payments are attributed to, and the ``cash`` they moved.
    """
    store = SQLiteStore(":memory:")
    checkpointer = _checkpointer(store)
    _open_a_position(checkpointer)

    for index in range(passes):
        if index == restart_before:
            checkpointer = _checkpointer(store)  # a new process, an empty memory
        for boundary in sorted(boundaries):
            checkpointer.checkpoint_funding(_accrual(boundary))

    settled = _AMOUNT * len(boundaries)  # one payment per distinct boundary, no more
    assert [position.funding for position in store.all_positions()] == [settled]
    account = store.load_account()
    assert account is not None
    assert account.cash == GENESIS + settled
    assert store.funding_mark("BTC") == max(boundaries) * _HOUR
