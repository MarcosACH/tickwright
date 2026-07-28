"""The ``Store`` contract, parametrized over every adapter (ADR-0019).

The store is the durability half of the crash-safety argument (ADR-0008): a
checkpointed ``Order`` must round-trip losslessly — state, quantities, reason,
transition history, and the applied-event dedup set — because recovery rebuilds
the saga from exactly this record. The accounting ledger (ADR-0043) is held to
the same bar, and to one more: its write is atomic across the order row, the
position rows and the account row together.

This suite states that contract once and runs it through each backend the
``store_backend`` fixture names, so ``SQLiteStore`` and ``PostgresStore`` are
held to identical semantics; only durability differs. That parity is why the
ledger grows cases here rather than per-backend ``SELECT``s: a column no read
surfaces is one the two backends could disagree about unobserved.

The Postgres arm carries the ``postgres`` marker and auto-skips when no server is
reachable (see ``conftest``), so ``uv run pytest`` stays hermetic by default.
"""

from decimal import Decimal

import pytest
from store_backends import PostgresBackend, SQLiteBackend

from tickwright.domain import (
    Account,
    AccountSpec,
    InvariantViolation,
    Order,
    OrderFilled,
    OrderState,
    OrderSubmitted,
    OrderType,
    Position,
    Side,
)

Backend = SQLiteBackend | PostgresBackend


def _order() -> Order:
    return Order(
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("2"),
        order_type=OrderType.MARKET,
    )


def _submitted() -> OrderSubmitted:
    return OrderSubmitted(
        ts_event=1,
        ts_init=1,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        venue_oid="oid-1",
    )


def _filled(trade_id: str = "v1") -> OrderFilled:
    return OrderFilled(
        ts_event=2,
        ts_init=2,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        trade_id=trade_id,
        quantity=Decimal("2"),
        price=Decimal("100"),
        cum_qty=Decimal("2"),
    )


def _account(*, genesis: str = "10000", ts_ns: int = 1_000) -> Account:
    return Account.open(
        AccountSpec(account_id="paper-default", genesis_collateral=Decimal(genesis)),
        ts_ns=ts_ns,
    )


def test_get_order_returns_none_for_an_unknown_cloid(store_backend: Backend) -> None:
    with store_backend.open() as store:
        assert store.get_order("0xnope") is None


def test_checkpointed_order_round_trips(store_backend: Backend) -> None:
    order = _order()
    order.apply(_submitted())
    with store_backend.open() as store:
        store.checkpoint(order, ts_ns=1_000)
        loaded = store.get_order("0xabc")

    assert loaded is not None
    assert loaded.cloid == "0xabc"
    assert loaded.strategy_id == "trivial"
    assert loaded.signal_id == "trivial:BTC:1"
    assert loaded.symbol == "BTC"
    assert loaded.side is Side.BUY
    assert loaded.quantity == Decimal("2")
    assert loaded.order_type is OrderType.MARKET
    assert loaded.state is OrderState.SUBMITTED
    assert loaded.venue_oid == "oid-1"


def test_every_saga_column_round_trips_under_a_distinct_value(store_backend: Backend) -> None:
    """One saga carrying a different value in every column, so no pair of columns
    can be swapped on the way to disk and still read back correct.

    The sibling of the position case below, and it earns its place for the same
    reason: the write tuple and the statement are generated from one column list
    (``_records``), and this is what proves the list is the *schema's* order and
    not merely self-consistent. ``cum_qty`` differs from ``quantity`` and the
    cancel trio is populated precisely because the happy-path round trip above
    leaves those six columns either equal or unasserted."""
    order = Order.restore(
        cloid="0xcafe",
        strategy_id="alpha",
        signal_id="alpha:ETH:7",
        symbol="ETH",
        side=Side.SELL,
        quantity=Decimal("9"),
        order_type=OrderType.LIMIT,
        state=OrderState.PARTIALLY_FILLED,
        cum_qty=Decimal("4"),
        venue_oid="oid-77",
        reason="reduce-only rejected leg",
        cancel_requested=True,
        cancel_requested_ts=5_555,
        cancel_signal_id="alpha:ETH:8",
        applied_event_ids=["evt-1", "evt-2"],
    )
    with store_backend.open() as store:
        store.checkpoint(order, ts_ns=1_000)

    with store_backend.open() as reopened:
        loaded = reopened.get_order("0xcafe")

    assert loaded is not None
    assert loaded.cloid == "0xcafe"
    assert loaded.strategy_id == "alpha"
    assert loaded.signal_id == "alpha:ETH:7"
    assert loaded.symbol == "ETH"
    assert loaded.side is Side.SELL
    assert loaded.quantity == Decimal("9")
    assert loaded.order_type is OrderType.LIMIT
    assert loaded.state is OrderState.PARTIALLY_FILLED
    assert loaded.cum_qty == Decimal("4")
    assert loaded.venue_oid == "oid-77"
    assert loaded.reason == "reduce-only rejected leg"
    assert loaded.cancel_requested is True
    assert loaded.cancel_requested_ts == 5_555
    assert loaded.cancel_signal_id == "alpha:ETH:8"
    assert loaded.applied_event_ids == frozenset({"evt-1", "evt-2"})


def test_recheckpointing_upserts_the_record_and_appends_history(store_backend: Backend) -> None:
    order = _order()
    with store_backend.open() as store:
        store.checkpoint(order, ts_ns=1_000)  # write-ahead PENDING
        order.apply(_submitted())
        store.checkpoint(order, ts_ns=2_000)
        order.apply(_filled())
        store.checkpoint(order, ts_ns=3_000)

        loaded = store.get_order("0xabc")
        assert loaded is not None
        assert loaded.state is OrderState.FILLED
        # Every transition left a durable trail entry (ADR-0008 checkpoint points).
        assert store.history("0xabc") == [
            (OrderState.PENDING, 1_000),
            (OrderState.SUBMITTED, 2_000),
            (OrderState.FILLED, 3_000),
        ]


def test_restored_order_still_dedups_a_pre_crash_fill(store_backend: Backend) -> None:
    order = _order()
    order.apply(_submitted())
    order.apply(_filled(trade_id="v1"))
    with store_backend.open() as store:
        store.checkpoint(order, ts_ns=1_000)
        loaded = store.get_order("0xabc")

    # The redelivered fill predates the "crash": still a no-op after restore.
    assert loaded is not None
    assert loaded.apply(_filled(trade_id="v1")) is False
    assert loaded.cum_qty == Decimal("2")


def test_durable_record_survives_close_and_reopen(store_backend: Backend) -> None:
    order = _order()
    order.apply(_submitted())
    with store_backend.open() as first:
        first.checkpoint(order, ts_ns=1_000)

    # A fresh store over the same durable backing reads the committed record.
    with store_backend.open() as reopened:
        loaded = reopened.get_order("0xabc")
        assert loaded is not None
        assert loaded.state is OrderState.SUBMITTED
        assert reopened.history("0xabc") == [(OrderState.SUBMITTED, 1_000)]


def test_close_is_idempotent(store_backend: Backend) -> None:
    store = store_backend.open()
    store.close()
    store.close()  # a second close is a no-op, never an error


def test_cancel_requested_marker_round_trips(store_backend: Backend) -> None:
    order = _order()
    order.apply(_submitted())  # a marker is state-independent; SUBMITTED is fine here
    order.request_cancel(signal_id="trivial:BTC:2", ts_ns=1_500)
    with store_backend.open() as store:
        store.checkpoint(order, ts_ns=2_000)
        loaded = store.get_order("0xabc")

    # Recovery must see the marker so an ack-lost cancel resolves, and the cancel's
    # own signal_id so the seq high-water-mark survives the restart (ADR-0026).
    assert loaded is not None
    assert loaded.cancel_requested is True
    assert loaded.cancel_requested_ts == 1_500
    assert loaded.cancel_signal_id == "trivial:BTC:2"


def test_default_order_round_trips_without_a_cancel_marker(store_backend: Backend) -> None:
    order = _order()
    order.apply(_submitted())
    with store_backend.open() as store:
        store.checkpoint(order, ts_ns=1_000)
        loaded = store.get_order("0xabc")

    assert loaded is not None
    assert loaded.cancel_requested is False
    assert loaded.cancel_requested_ts is None
    assert loaded.cancel_signal_id is None


def test_all_orders_returns_every_saga_ordered_by_cloid(store_backend: Backend) -> None:
    with store_backend.open() as store:
        for cloid in ("0xccc", "0xaaa", "0xbbb"):
            order = _order()
            order.cloid = cloid
            store.checkpoint(order, ts_ns=1_000)

        assert [order.cloid for order in store.all_orders()] == ["0xaaa", "0xbbb", "0xccc"]


def test_strategy_snapshot_round_trips_and_upserts(store_backend: Backend) -> None:
    with store_backend.open() as store:
        assert store.load_strategy_snapshot("sma") is None

        store.save_strategy_snapshot("sma", b'{"window": 5}', ts_ns=1_000)
        store.save_strategy_snapshot("sma", b'{"window": 9}', ts_ns=2_000)  # latest wins
        store.save_strategy_snapshot("grid", b"levels", ts_ns=2_000)

    with store_backend.open() as reopened:
        assert reopened.load_strategy_snapshot("sma") == b'{"window": 9}'
        assert reopened.load_strategy_snapshot("grid") == b"levels"


def test_kill_switch_round_trips_and_is_none_until_written(store_backend: Backend) -> None:
    with store_backend.open() as store:
        assert store.load_kill_switch() is None

        store.save_kill_switch(tripped=True, reason="drawdown", ts_ns=1_000)

    # Sticky by design: the tripped halt must outlive the process that set it.
    with store_backend.open() as reopened:
        state = reopened.load_kill_switch()
        assert state is not None
        assert state.tripped is True
        assert state.reason == "drawdown"
        assert state.ts_ns == 1_000


def test_account_round_trips_and_is_none_until_checkpointed(store_backend: Backend) -> None:
    """``None`` is the live-first-run / paper-seed-genesis state (ADR-0043 §9), and
    the restored row carries both the opening declaration and the accrued line."""
    with store_backend.open() as store:
        assert store.load_account() is None

        account = _account(genesis="10000", ts_ns=1_000)
        account.accrue_realized(Decimal("250"), event_id="0xabc:fill:v1")
        store.checkpoint_ledger(account=account, ts_ns=2_000)

    with store_backend.open() as reopened:
        loaded = reopened.load_account()
        assert loaded is not None
        assert loaded.account_id == "paper-default"
        assert loaded.genesis_collateral == Decimal("10000")
        assert loaded.genesis_ts_ns == 1_000
        assert loaded.cash == Decimal("10250")


def test_position_round_trips(store_backend: Backend) -> None:
    """Every Tier-1 line survives the round trip, each column distinct so a
    mis-ordered write tuple cannot pass by coincidence."""
    position = Position(
        strategy_id="trivial",
        symbol="BTC",
        signed_size=Decimal("-1.5"),
        entry_price=Decimal("50000.25"),
        realized_pnl=Decimal("-12.5"),
        fees=Decimal("3.75"),
        funding=Decimal("0.125"),
        isolated_collateral=Decimal("7500"),
    )
    with store_backend.open() as store:
        store.checkpoint_ledger(account=_account(), positions=[position], ts_ns=2_000)

    with store_backend.open() as reopened:
        (loaded,) = reopened.all_positions()

    assert loaded.strategy_id == "trivial"
    assert loaded.symbol == "BTC"
    assert loaded.signed_size == Decimal("-1.5")
    assert loaded.entry_price == Decimal("50000.25")
    assert loaded.realized_pnl == Decimal("-12.5")
    assert loaded.fees == Decimal("3.75")
    assert loaded.funding == Decimal("0.125")
    assert loaded.isolated_collateral == Decimal("7500")


def test_all_positions_returns_the_unattributed_partition_unfiltered(
    store_backend: Backend,
) -> None:
    """ADR-0034's Σ-invariant holds by construction only if the reserved
    partition is restored with everything else, so filtering belongs at the
    ``Portfolio`` seam and never here (ADR-0043 §9). ``None`` is the partition
    in memory; ``__unattributed__`` is what it is on disk (§2)."""
    attributed = Position(strategy_id="trivial", symbol="BTC", signed_size=Decimal("2"))
    unattributed = Position(strategy_id=None, symbol="BTC", signed_size=Decimal("-0.5"))
    with store_backend.open() as store:
        store.checkpoint_ledger(
            account=_account(), positions=[attributed, unattributed], ts_ns=2_000
        )

    with store_backend.open() as reopened:
        loaded = reopened.all_positions()

    assert {position.strategy_id: position.signed_size for position in loaded} == {
        "trivial": Decimal("2"),
        None: Decimal("-0.5"),
    }


def test_recheckpointing_a_position_upserts_in_place(store_backend: Backend) -> None:
    """Current-state rows, not an event log (ADR-0043 §1): a second write moves
    the row rather than appending one. The count is the assertion that matters
    for the unattributed partition — under a ``NULL`` key SQLite's upsert would
    have inserted a second row rather than updating (§2)."""
    with store_backend.open() as store:
        store.checkpoint_ledger(
            account=_account(),
            positions=[
                Position(strategy_id="trivial", symbol="BTC", signed_size=Decimal("2")),
                Position(strategy_id=None, symbol="BTC", signed_size=Decimal("-0.5")),
            ],
            ts_ns=2_000,
        )
        store.checkpoint_ledger(
            account=_account(),
            positions=[
                Position(strategy_id="trivial", symbol="BTC", signed_size=Decimal("3")),
                Position(strategy_id=None, symbol="BTC", signed_size=Decimal("-1.5")),
            ],
            ts_ns=3_000,
        )
        loaded = store.all_positions()

    assert len(loaded) == 2
    assert {position.strategy_id: position.signed_size for position in loaded} == {
        "trivial": Decimal("3"),
        None: Decimal("-1.5"),
    }


def test_money_round_trips_exactly_in_representation(store_backend: Backend) -> None:
    """Every money column is ``TEXT``, written ``str`` and read ``Decimal``, so the
    round trip is exact in *representation* and not merely in numeric value
    (ADR-0043 §7). A ``NUMERIC`` column normalises: it returns ``1000`` for
    ``1E+3`` and ``0.00000000`` for ``0E-8`` — numerically lossless, and lossy in
    exactly the way ``str(Decimal)`` is not. Asserted on the *text*, since
    ``Decimal("1.10") == Decimal("1.1")``, which is what makes this silent."""
    position = Position(
        strategy_id="trivial",
        symbol="BTC",
        signed_size=Decimal("1.10"),
        entry_price=Decimal("0E-8"),
        realized_pnl=Decimal("-0"),
        fees=Decimal("1E+3"),
        funding=Decimal("-0.0"),
        isolated_collateral=Decimal("1.000"),
    )
    account = Account.restore(
        account_id="paper-default",
        genesis_collateral=Decimal("2.50"),
        genesis_ts_ns=1_000,
        cash=Decimal("1E+4"),
    )
    with store_backend.open() as store:
        store.checkpoint_ledger(account=account, positions=[position], ts_ns=2_000)

    with store_backend.open() as reopened:
        (loaded,) = reopened.all_positions()
        restored_account = reopened.load_account()

    assert str(loaded.signed_size) == "1.10"
    assert str(loaded.entry_price) == "0E-8"
    assert str(loaded.realized_pnl) == "-0"
    assert str(loaded.fees) == "1E+3"
    assert str(loaded.funding) == "-0.0"
    assert str(loaded.isolated_collateral) == "1.000"

    assert restored_account is not None
    assert str(restored_account.genesis_collateral) == "2.50"
    assert str(restored_account.cash) == "1E+4"


def test_a_flat_position_keeps_its_history_across_the_round_trip(store_backend: Backend) -> None:
    """A flat record is still a record (ADR-0041 §3): the exposure is gone, the
    realized, fee and funding lines it left behind are not, so a restart must
    not read them back as a fresh partition."""
    flat = Position(
        strategy_id="trivial",
        symbol="BTC",
        signed_size=Decimal("0"),
        realized_pnl=Decimal("125.5"),
        fees=Decimal("2"),
        funding=Decimal("-0.5"),
    )
    with store_backend.open() as store:
        store.checkpoint_ledger(account=_account(), positions=[flat], ts_ns=2_000)

    with store_backend.open() as reopened:
        (loaded,) = reopened.all_positions()

    assert loaded.is_flat
    assert loaded.realized_pnl == Decimal("125.5")
    assert loaded.fees == Decimal("2")
    assert loaded.funding == Decimal("-0.5")
    # The entry column goes to disk ``NULL`` on a flat row (ADR-0043 §3) and
    # comes back ``0``, which is what ADR-0041 §3 reads through the seam. Both
    # backends make that trip here; that the value written is ``NULL`` and not
    # the string ``"0"`` is pinned in ``test_records``, where it is observable.
    assert loaded.entry_price == Decimal("0")


def test_the_accounts_opening_declaration_survives_a_later_checkpoint(
    store_backend: Backend,
) -> None:
    """``account_id``, ``genesis_collateral`` and ``genesis_ts_ns`` are written
    once with the row and excluded from the upsert's update list (ADR-0043 §3).
    An instant nobody recorded is not recoverable afterwards, unlike a sum — so
    a second checkpoint carrying different values must not move them, and only
    the cash line follows the incoming account."""
    with store_backend.open() as store:
        store.checkpoint_ledger(account=_account(genesis="10000", ts_ns=1_000), ts_ns=1_000)

        moved = _account(genesis="99999", ts_ns=8_888)
        moved.accrue_realized(Decimal("-500"), event_id="0xabc:fill:v1")
        store.checkpoint_ledger(account=moved, ts_ns=3_000)

        loaded = store.load_account()

    assert loaded is not None
    assert loaded.genesis_collateral == Decimal("10000")
    assert loaded.genesis_ts_ns == 1_000
    assert loaded.cash == Decimal("99499")


def test_checkpoint_ledger_writes_the_order_with_the_ledger(store_backend: Backend) -> None:
    """A fill mutates the order row *and* the ledger, and as two transactions
    either ordering is unsound (ADR-0043 §4): checkpoint-first loses the fill
    from the ledger on a crash, ledger-first double-counts it. So they ride one
    call, and the order row it carries is the one ``checkpoint`` would write —
    same record, same appended transition trail."""
    order = _order()
    order.apply(_submitted())
    order.apply(_filled())
    position = Position(strategy_id="trivial", symbol="BTC")
    position.apply(_filled(), side=Side.BUY)

    with store_backend.open() as store:
        store.checkpoint_ledger(order=order, account=_account(), positions=[position], ts_ns=2_000)

    with store_backend.open() as reopened:
        loaded_order = reopened.get_order("0xabc")
        (loaded_position,) = reopened.all_positions()

        assert loaded_order is not None
        assert loaded_order.state is OrderState.FILLED
        assert loaded_order.cum_qty == Decimal("2")
        assert reopened.history("0xabc") == [(OrderState.FILLED, 2_000)]
        assert loaded_position.signed_size == Decimal("2")
        assert loaded_position.entry_price == Decimal("100")


def test_a_refused_ledger_write_leaves_no_part_of_it_durable(store_backend: Backend) -> None:
    """One transaction across every aggregate handed to it (ADR-0043 §4): a
    failure part-way leaves none of them, and raises rather than running on
    (ADR-0014). Half a fill is the state the atomic write exists to make
    unreachable — on paper nothing ever heals it, because the in-process venue
    holds no position state and the store is the sole authority.

    The refused row is a position with no symbol: ``NOT NULL`` is the constraint
    both backends enforce identically, standing in for any write the store
    cannot make durable."""
    good = Position(strategy_id="trivial", symbol="BTC", signed_size=Decimal("2"))
    refused = Position(strategy_id="trivial", symbol=None, signed_size=Decimal("1"))  # type: ignore[arg-type]

    with store_backend.open() as store:
        with pytest.raises(InvariantViolation):
            store.checkpoint_ledger(
                order=_order(), account=_account(), positions=[good, refused], ts_ns=2_000
            )

    with store_backend.open() as reopened:
        assert reopened.load_account() is None
        assert reopened.all_positions() == []
        assert reopened.get_order("0xabc") is None


def test_a_store_that_cannot_reach_its_backend_raises_invariant_violation(
    store_backend: Backend,
) -> None:
    """One error contract for the whole seam, not one per method (ADR-0014).

    ``checkpoint_ledger`` already promised ``InvariantViolation``; every other
    member let the driver's own exception cross the seam, so the same failure had
    two types depending on which method met it. That difference is load-bearing
    rather than cosmetic: ``InvariantViolation`` is what pierces the engine's
    strategy-containment net, and a raw ``sqlite3.Error`` reaching that net would
    be filed as a strategy bug and survived, rather than faulting the engine.

    A closed store stands in for any unreachable backend: both drivers raise from
    their own ``Error`` base, which is the whole of what each adapter contributes
    to this rule."""
    store = store_backend.open()
    store.close()

    with pytest.raises(InvariantViolation):
        store.checkpoint(_order(), ts_ns=1_000)
    with pytest.raises(InvariantViolation):
        store.get_order("0xabc")
    with pytest.raises(InvariantViolation):
        store.all_positions()
    with pytest.raises(InvariantViolation):
        store.has_orders()
    with pytest.raises(InvariantViolation):
        store.load_account()
    with pytest.raises(InvariantViolation):
        store.checkpoint_ledger(account=_account(), ts_ns=1_000)


def test_the_funding_mark_is_absent_until_written_and_then_advances(
    store_backend: Backend,
) -> None:
    """The ledger's one durable idempotency record (ADR-0043 §5.2), keyed by
    symbol because that is the grain of the key it is half of. The absence of a
    row is the "never accrued" state — distinct from a boundary applied at epoch
    ``0``, which is why the column is ``NOT NULL`` and the read returns ``None``.

    It rides ``checkpoint_ledger`` rather than a call of its own so the advance
    lands in the same transaction as the funding line it guards."""
    with store_backend.open() as store:
        assert store.funding_mark("BTC") is None

        store.checkpoint_ledger(account=_account(), funding_mark=("BTC", 1_700), ts_ns=2_000)

        assert store.funding_mark("BTC") == 1_700
        assert store.funding_mark("ETH") is None  # one row per traded symbol

        store.checkpoint_ledger(account=_account(), funding_mark=("BTC", 1_800), ts_ns=3_000)

    with store_backend.open() as reopened:
        assert reopened.funding_mark("BTC") == 1_800


def test_has_orders_reports_whether_any_saga_history_exists(store_backend: Backend) -> None:
    """A ``bool``, and a member of its own rather than a reuse of ``all_orders()``
    (ADR-0043 §9): the startup check runs *before* ``cache.rebuild()``, and
    answering an existence question with the mass-read would deserialize every
    saga in the store twice on every start — on the recovery path, where the
    engine is least able to afford it. The ``bool`` also keeps the check honest
    about what it is entitled to know: nothing about the orders themselves."""
    with store_backend.open() as store:
        assert store.has_orders() is False

        store.checkpoint(_order(), ts_ns=1_000)

        assert store.has_orders() is True

    with store_backend.open() as reopened:
        assert reopened.has_orders() is True


def test_a_store_with_order_history_and_no_ledger_opens_and_reads_it_empty(
    store_backend: Backend,
) -> None:
    """The DDL is purely additive — three new tables, no change to the existing
    three — so a store written before the ledger gains empty tables on next open
    and that is the entire migration (ADR-0043 §8). No schema-version table, no
    migration framework; the cost named rather than hidden is that the first
    *non*-additive change needs per-backend handling.

    The state this reads is the one a previous release leaves behind, and it is
    exactly the pair the paper-path startup refusal keys on: order history with
    no account row. Refusing it is a later slice's; opening it is this one's."""
    with store_backend.open() as store:
        store.checkpoint(_order(), ts_ns=1_000)

    with store_backend.open() as reopened:
        assert reopened.has_orders() is True
        assert reopened.load_account() is None
        assert reopened.all_positions() == []
        assert reopened.funding_mark("BTC") is None
