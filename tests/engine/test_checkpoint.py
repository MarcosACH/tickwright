"""``Checkpointer`` — the one writer of both read-models, over one ``Store``.

Exercised through its write verbs rather than its internals: the durable side is
read back from the store it was constructed with, and the in-memory side through
the two projections it lends out. That pairing is the whole of what the type
promises — a fill's order row and its ledger rows reach *one* store in one
transaction (ADR-0043 §4), which no caller can any longer get wrong by wiring
two.
"""

from collections.abc import Sequence
from decimal import Decimal

import pytest
from ledgers import GENESIS

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Account,
    AccountSpec,
    FundingAccrual,
    InvariantViolation,
    Order,
    OrderFillEvent,
    OrderState,
    Position,
    Side,
)
from tickwright.domain.enums import OrderType
from tickwright.engine.checkpoint import Checkpointer
from tickwright.observability import NamedEvent
from tickwright.observability.testing import capture_events

_HOUR = 3_600_000_000_000
"""One epoch-aligned funding boundary — the unit the accrual cases step in."""

SPEC = AccountSpec(account_id="paper-default", genesis_collateral=GENESIS)
"""The paper-shaped account every case here opens against — declared genesis, so
``recover`` seeds rather than waits for a venue to report one (ADR-0043 §10)."""

LIVE_SPEC = AccountSpec(account_id="hyperliquid-testnet-0xabc", genesis_collateral=None)
"""The ingested shape (ADR-0042 §6), for the one case below whose subject is a
ledger the seed deliberately leaves unopened. Three segments against paper's two,
so a row written under one is never confusable with the other's."""


def _checkpointer(store: SQLiteStore, *, spec: AccountSpec = SPEC) -> Checkpointer:
    return Checkpointer(spec=spec, store=store, clock=ManualClock(start_ns=1_000))


def _submitted_order(*, quantity: str = "0.5", side: Side = Side.BUY, seq: int = 1) -> Order:
    """A saga at the one state a fill legally advances from (``_LEGAL_TRANSITIONS``).

    ``side`` and ``seq`` vary only for the one case below needing a **second**
    saga — the close that takes the symbol flat — since a partition is advanced
    by the fills of distinct orders, never by one order filled twice. ``seq``
    moves the cloid and the signal id together so the pair can never collide.
    """
    return Order(
        cloid=f"0xtrivial-{seq}",
        strategy_id="trivial",
        signal_id=f"trivial:BTC:{seq}",
        symbol="BTC",
        side=side,
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


def _accrual(*, amount: str, boundary: int, symbol: str = "BTC") -> FundingAccrual:
    """One settled boundary, as an ``Exchange`` adapter produced it — signed as
    the venue reports it, ``< 0`` paid (ADR-0037)."""
    return FundingAccrual(
        ts_event=boundary,
        ts_init=boundary,
        account_id=SPEC.account_id,
        symbol=symbol,
        boundary_ts_ns=boundary,
        amount=Decimal(amount),
    )


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


def test_an_accrual_makes_the_funding_line_and_its_mark_durable_together() -> None:
    """The mark lands in the same write as the line it guards (ADR-0043 §5.2).

    They are handed to the store as one ``checkpoint_ledger`` call rather than
    two, so there is no crash point between them: the mark can never be ahead of
    a payment that was not recorded, nor behind one that was. Carrying no order
    is the other half of the shape — funding has no carrier fill (ADR-0037), so
    the write that applies it touches the ledger alone.

    Read back from the store rather than from the projection, because what is
    being asserted is durability: the projection moved in memory the moment the
    fold ran, and that is precisely what a crash would have discarded.
    """
    store = SQLiteStore(":memory:")
    checkpointer = _checkpointer(store)
    checkpointer.recover()
    order = _submitted_order()
    checkpointer.checkpoint_fill(
        order, _fill(order, trade_id="f1", quantity="0.5"), side=order.side
    )

    checkpointer.checkpoint_funding(_accrual(amount="-3.5", boundary=_HOUR))

    assert store.funding_mark("BTC") == _HOUR
    assert [position.funding for position in store.all_positions()] == [Decimal("-3.5")]
    account = store.load_account()
    assert account is not None
    assert account.cash == GENESIS - Decimal("3.5")


def test_an_accrual_whose_symbol_went_flat_moves_cash_and_attributes_nothing() -> None:
    """The one place the cash line and the funding lines come apart, pinned.

    The venue computes the amount from the account net it read **at the
    boundary**; the projection folds it across the partitions held **when it
    arrives**. A fill closing the symbol in between leaves nothing to attribute
    to, and ADR-0043 §5.2 has already decided which of the two questions wins:
    the gate is read before the split so that whether a payment *happened* is
    answered independently of who it is charged to. So ``cash`` moves whole, the
    funding line stays at zero, and the mark advances in the same write — which
    means no later pass revisits it.

    Asserted here rather than left to the docstring because it is the ledger a
    reader would otherwise have to discover the hard way: ``Σ funding lines``
    genuinely falls short of the cash movement, and that is a recorded decision
    rather than a bug. It is unreachable on the hermetic path — ``ReplayFeed``
    yields before it publishes, so the generator's read and its
    drain-to-quiescence publish are one uninterrupted stretch — and reachable
    wherever the generator wakes at an arbitrary point instead: a ``LiveClock``
    paper run, or ``KafkaBus`` dispatching the accrual behind queued fills. The
    residue is what [#189](https://github.com/MarcosACH/tickwright/issues/189)'s
    reserved partition will absorb; until then the account line carries it, which
    is the direction that keeps ``cash`` faithful to what was actually paid.

    Driven at this seam rather than through the venue because the interleaving
    is what is under test, and only a caller holding both verbs can place a
    close *between* a boundary's basis and its application.
    """
    store = SQLiteStore(":memory:")
    checkpointer = _checkpointer(store)
    checkpointer.recover()
    opening = _submitted_order()
    checkpointer.checkpoint_fill(
        opening, _fill(opening, trade_id="f1", quantity="0.5"), side=opening.side
    )
    closing = _submitted_order(side=Side.SELL, seq=2)
    checkpointer.checkpoint_fill(
        closing, _fill(closing, trade_id="f2", quantity="0.5"), side=closing.side
    )

    with capture_events() as logs:
        checkpointer.checkpoint_funding(_accrual(amount="-2.1", boundary=_HOUR))

    assert [position.funding for position in store.all_positions()] == [Decimal("0")]
    account = store.load_account()
    assert account is not None
    assert account.cash == GENESIS - Decimal("2.1")  # the payment still happened
    assert store.funding_mark("BTC") == _HOUR  # and is never revisited
    # Nothing moved, so nothing is announced: the movement is invisible in the
    # trail too, which is the whole of what #189 has left to pick up.
    assert logs == []


def test_a_dropped_accrual_writes_nothing_at_all() -> None:
    """A boundary already applied leaves the store exactly as it was.

    The gate's job is to make a re-delivery a no-op, and "no-op" has to reach the
    durable side too: a dropped accrual that still wrote would re-stamp the
    account row and could only be distinguished from a real payment by reading
    the number. So the second delivery is asserted to move neither the funding
    line nor cash — the double-count ADR-0043 §5.2 exists to prevent, reached
    through the path that actually re-delivers.
    """
    store = SQLiteStore(":memory:")
    checkpointer = _checkpointer(store)
    checkpointer.recover()
    order = _submitted_order()
    checkpointer.checkpoint_fill(
        order, _fill(order, trade_id="f1", quantity="0.5"), side=order.side
    )
    checkpointer.checkpoint_funding(_accrual(amount="-3.5", boundary=_HOUR))

    checkpointer.checkpoint_funding(_accrual(amount="-3.5", boundary=_HOUR))

    assert [position.funding for position in store.all_positions()] == [Decimal("-3.5")]
    account = store.load_account()
    assert account is not None
    assert account.cash == GENESIS - Decimal("3.5")


def test_an_applied_accrual_announces_every_partition_it_moved() -> None:
    """An accrual moving a non-flat record is a ``position.changed`` (ADR-0045 §2).

    The catalog is the **only** place a position change is observable from
    outside — it is an output derived from an input already on the bus, never a
    bus event of its own (ADR-0045 §1) — so a funding line that moved with no
    record moved invisibly. That matters more here than on the fill path, not
    less: funding is the one accounting input with no carrier, so there is no
    ``order.*`` record beside it that an operator could read the movement off
    instead.

    Announced *behind* the write, like the fill path's: a record naming a change
    a crash could still undo would report a payment the ledger never kept. The
    capture opens after the opening fill so what it holds is the accrual's alone.
    """
    store = SQLiteStore(":memory:")
    checkpointer = _checkpointer(store)
    checkpointer.recover()
    order = _submitted_order()
    checkpointer.checkpoint_fill(
        order, _fill(order, trade_id="f1", quantity="0.5"), side=order.side
    )

    with capture_events() as logs:
        checkpointer.checkpoint_funding(_accrual(amount="-3.5", boundary=_HOUR))

    assert [log["event"] for log in logs] == [NamedEvent.POSITION_CHANGED.value]
    assert logs[0]["symbol"] == "BTC"
    assert logs[0]["strategy_id"] == "trivial"
    assert logs[0]["funding"] == "-3.5"  # what moved, where the fill path reports size


def test_a_dropped_accrual_announces_nothing_either() -> None:
    """The drop is whole, and "whole" reaches the trail as well as the store.

    A re-delivery that still emitted a ``position.changed`` would put a payment
    in the operator's record that the ledger deliberately did not take — the
    double-count ADR-0043 §5.2 prevents, reappearing in the one place left that
    a reader would believe.
    """
    store = SQLiteStore(":memory:")
    checkpointer = _checkpointer(store)
    checkpointer.recover()
    order = _submitted_order()
    checkpointer.checkpoint_fill(
        order, _fill(order, trade_id="f1", quantity="0.5"), side=order.side
    )
    checkpointer.checkpoint_funding(_accrual(amount="-3.5", boundary=_HOUR))

    with capture_events() as logs:
        checkpointer.checkpoint_funding(_accrual(amount="-3.5", boundary=_HOUR))

    assert logs == []


class _FundingRefusingStore(SQLiteStore):
    """The real store, refusing exactly the ledger write that carries a mark.

    Narrowed to the funding write so the opening fill still lands: what is under
    test is a refusal of *this* write, against a ledger that was healthy up to
    it, rather than a store that was never usable.
    """

    def checkpoint_ledger(
        self,
        *,
        account: Account,
        positions: Sequence[Position] = (),
        order: Order | None = None,
        funding_mark: tuple[str, int] | None = None,
        ts_ns: int,
    ) -> None:
        if funding_mark is not None:
            raise InvariantViolation("disk is full")
        super().checkpoint_ledger(account=account, positions=positions, order=order, ts_ns=ts_ns)


def test_a_refused_funding_write_is_relabelled_with_the_boundary_it_lost() -> None:
    """The diagnosis an operator gets when the funding write cannot be made.

    ``InvariantViolation`` is the whole of the ``Store``'s error contract
    (ADR-0019), so it is the one type this seam relabels — and the label has to
    carry what the store's own message cannot: *which* boundary on *which* symbol
    failed to land. With #226 open that message is the only account of a
    generator that then dies, so it is asserted rather than assumed, chained
    cause included.
    """
    store = _FundingRefusingStore(":memory:")
    checkpointer = _checkpointer(store)
    checkpointer.recover()
    order = _submitted_order()
    checkpointer.checkpoint_fill(
        order, _fill(order, trade_id="f1", quantity="0.5"), side=order.side
    )

    with pytest.raises(InvariantViolation, match="funding checkpoint write failed for BTC") as exc:
        checkpointer.checkpoint_funding(_accrual(amount="-3.5", boundary=_HOUR))

    assert str(_HOUR) in str(exc.value)
    assert isinstance(exc.value.__cause__, InvariantViolation)
    assert "disk is full" in str(exc.value.__cause__)


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
