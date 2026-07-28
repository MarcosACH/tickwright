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

from store_backends import PostgresBackend, SQLiteBackend

from tickwright.domain import (
    Account,
    AccountSpec,
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
