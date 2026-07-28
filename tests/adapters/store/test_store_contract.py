"""The ``Store`` contract, parametrized over every adapter (ADR-0019).

The store is the durability half of the crash-safety argument (ADR-0008): a
checkpointed ``Order`` must round-trip losslessly — state, quantities, reason,
transition history, and the applied-event dedup set — because recovery rebuilds
the saga from exactly this record. This suite states that contract once and runs
it through each backend the ``store_backend`` fixture names, so ``SQLiteStore``
and ``PostgresStore`` are held to identical semantics; only durability differs.

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
