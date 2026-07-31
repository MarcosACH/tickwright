"""``Checkpointer`` — the one writer of both read-models, over one ``Store``.

Exercised through its write verbs rather than its internals: the durable side is
read back from the store it was constructed with, and the in-memory side through
the two projections it lends out. That pairing is the whole of what the type
promises — a fill's order row and its ledger rows reach *one* store in one
transaction (ADR-0043 §4), which no caller can any longer get wrong by wiring
two.
"""

from decimal import Decimal

from ledgers import GENESIS

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Account,
    AccountSpec,
    Order,
    OrderFillEvent,
    OrderState,
    Side,
)
from tickwright.domain.enums import OrderType
from tickwright.engine.checkpoint import Checkpointer

SPEC = AccountSpec(account_id="paper-default", genesis_collateral=GENESIS)
"""The paper-shaped account every case here opens against — declared genesis, so
``recover`` seeds rather than waits for a venue to report one (ADR-0043 §10)."""

LIVE_SPEC = AccountSpec(account_id="hyperliquid-testnet-0xabc", genesis_collateral=None)
"""The ingested shape (ADR-0042 §6), for the one case below whose subject is a
ledger the seed deliberately leaves unopened. Three segments against paper's two,
so a row written under one is never confusable with the other's."""


def _checkpointer(store: SQLiteStore, *, spec: AccountSpec = SPEC) -> Checkpointer:
    return Checkpointer(spec=spec, store=store, clock=ManualClock(start_ns=1_000))


def _submitted_order(*, quantity: str = "0.5") -> Order:
    """A saga at the one state a fill legally advances from (``_LEGAL_TRANSITIONS``)."""
    return Order(
        cloid="0xtrivial-1",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal(quantity),
        order_type=OrderType.MARKET,
        state=OrderState.SUBMITTED,
    )


def _fill(order: Order, *, trade_id: str, quantity: str, price: str = "42000") -> OrderFillEvent:
    """The canonical event ``Order`` itself mints, so the saga and the event this
    hands the checkpointer can never disagree the way a fabricated pair could."""
    event = order.record_fill(
        trade_id=trade_id,
        quantity=Decimal(quantity),
        price=Decimal(price),
        ts_event=1_000,
        ts_init=1_000,
    )
    assert event is not None
    return event


def test_a_fill_moves_the_one_store_and_both_read_models_together() -> None:
    """The reason the type exists: the order row and the ledger rows are written
    to the *same* store, and both projections are readable behind that write.

    Asserted by reading the durable side back from the single store handed in —
    after this change there is no second store a divergent projection could have
    written to, which is what makes the atomicity structural rather than a
    convention held at the wiring site.
    """
    store = SQLiteStore(":memory:")
    checkpointer = _checkpointer(store)
    order = _submitted_order()
    event = _fill(order, trade_id="f1", quantity="0.5")

    checkpointer.checkpoint_fill(order, event, side=order.side)

    record = store.get_order(order.cloid)
    assert record is not None
    assert record.state is OrderState.FILLED
    assert [
        (position.strategy_id, position.symbol, position.signed_size, position.entry_price)
        for position in store.all_positions()
    ] == [("trivial", "BTC", Decimal("0.5"), Decimal("42000"))]
    account = store.load_account()
    assert account is not None
    assert account.cash == GENESIS  # An opening fill realizes nothing.

    assert checkpointer.cache.get_order(order.cloid) is order
    position = checkpointer.portfolio.position("BTC", strategy_id="trivial")
    assert position is not None
    assert position.size == Decimal("0.5")


def test_a_fill_opens_the_durable_ledger_and_the_projection_says_so() -> None:
    """A fill's write is a **third** way the account row comes into being, beside
    paper's genesis seed and live's materialisation — ``account`` is required on
    ``checkpoint_ledger`` because every mutation moves cash (ADR-0043 §9).

    So "is the ledger open?" is a question about the *row*, whoever wrote it
    (ADR-0043 §6: the barrier reads the venue "solely to create the row when
    absent"), and this is the writer that does not go through either opener. The
    whole ordering rationale for the barrier's two steps rests on the fallback
    being real — a rebuild that ran first would create the row, and the
    materialisation behind it *declines to overwrite* rather than resetting the
    cash line a fill just moved. A predicate that missed this writer would answer
    "not open" against a populated row and make that fallback a fiction.

    Live-shaped on purpose: paper's row exists from ``recover()`` onward, so it
    is the ingested shape that can reach a fill with no row yet at all.
    """
    store = SQLiteStore(":memory:")
    checkpointer = _checkpointer(store, spec=LIVE_SPEC)
    checkpointer.recover()
    assert checkpointer.portfolio.is_opened() is False  # live seeds nothing
    order = _submitted_order()
    event = _fill(order, trade_id="f1", quantity="0.5")

    checkpointer.checkpoint_fill(order, event, side=order.side)

    assert store.load_account() is not None
    assert checkpointer.portfolio.is_opened() is True


def test_a_non_fill_transition_writes_the_order_row_and_no_ledger_row() -> None:
    """A fill is the only transition that moves the ledger, so every other one
    takes the narrow ``Store.checkpoint`` (ADR-0043 §4).

    Pinned on the *absence* of ledger rows rather than on which store method ran:
    what a reader needs to believe is that a cancel cannot silently move cash,
    not which call spelled that.
    """
    store = SQLiteStore(":memory:")
    checkpointer = _checkpointer(store)
    order = _submitted_order()

    checkpointer.checkpoint(order)

    record = store.get_order(order.cloid)
    assert record is not None
    assert record.state is OrderState.SUBMITTED
    assert store.all_positions() == []
    assert store.load_account() is None  # untouched: no ledger write happened
    assert checkpointer.cache.get_order(order.cloid) is order


class _RecoveryOrderStore(SQLiteStore):
    """The real store, recording the two recovery reads whose order is the
    contract: the ledger's ``load_account`` and the ``Cache``'s ``all_orders``.

    The ordering has no other observation port — neither step leaves a
    distinguishing durable trace — so the seam they share is where it shows.
    Recorded, not simulated: every call still reaches the real store underneath.
    """

    def __init__(self, timeline: list[str]) -> None:
        super().__init__(":memory:")
        self._timeline = timeline

    def load_account(self) -> Account | None:
        self._timeline.append("ledger.load_account")
        return super().load_account()

    def all_orders(self) -> list[Order]:
        self._timeline.append("cache.all_orders")
        return super().all_orders()


def test_recovery_reads_the_ledger_before_it_rebuilds_the_order_cache() -> None:
    """The ledger recovers first, ahead of the order cache (ADR-0043 §6/§10).

    Load-bearing rather than tidy: the ledger asks for the account row and the
    partitions behind it, where the rebuild deserializes every saga in the store
    — partitions are bounded by strategy × symbol, sagas by all the history the
    store holds. Behind the rebuild, a restart that must not trade at all would
    pay that mass read before finding out.

    ``tests/engine/test_runner_e2e.py`` pins that the *runner* recovers through
    this at all; the rule itself is asserted here, where it is now owned — so a
    future runner refactor cannot lose the ordering without a red test naming it.
    """
    timeline: list[str] = []
    checkpointer = _checkpointer(_RecoveryOrderStore(timeline))

    checkpointer.recover()

    assert timeline == ["ledger.load_account", "cache.all_orders"]
