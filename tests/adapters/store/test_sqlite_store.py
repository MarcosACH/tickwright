"""``SQLiteStore`` — the durable saga checkpoint store (ADR-0019).

The store is the durability half of the crash-safety argument (ADR-0008): a
checkpointed ``Order`` must round-trip losslessly — state, quantities, reason,
transition history, and the applied-event dedup set — because recovery rebuilds
the saga from exactly this record. File-backed and ``:memory:`` behave alike;
only the file survives a close-and-reopen.
"""

from decimal import Decimal
from pathlib import Path

from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    Order,
    OrderFilled,
    OrderState,
    OrderSubmitted,
    OrderType,
    Side,
)


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


def test_get_order_returns_none_for_an_unknown_cloid() -> None:
    store = SQLiteStore(":memory:")
    assert store.get_order("0xnope") is None


def test_recheckpointing_upserts_the_record_and_appends_history() -> None:
    store = SQLiteStore(":memory:")
    order = _order()
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


def test_restored_order_still_dedups_a_pre_crash_fill() -> None:
    store = SQLiteStore(":memory:")
    order = _order()
    order.apply(_submitted())
    order.apply(_filled(trade_id="v1"))
    store.checkpoint(order, ts_ns=1_000)

    loaded = store.get_order("0xabc")

    # The redelivered fill predates the "crash": still a no-op after restore.
    assert loaded is not None
    assert loaded.apply(_filled(trade_id="v1")) is False
    assert loaded.cum_qty == Decimal("2")


def test_file_backed_store_survives_close_and_reopen(tmp_path: Path) -> None:
    path = tmp_path / "saga.db"
    first = SQLiteStore(path)
    order = _order()
    order.apply(_submitted())
    first.checkpoint(order, ts_ns=1_000)
    first.close()

    reopened = SQLiteStore(path)
    loaded = reopened.get_order("0xabc")
    assert loaded is not None
    assert loaded.state is OrderState.SUBMITTED
    assert reopened.history("0xabc") == [(OrderState.SUBMITTED, 1_000)]


def test_store_is_a_context_manager_that_closes_on_exit(tmp_path: Path) -> None:
    path = tmp_path / "saga.db"
    order = _order()
    order.apply(_submitted())
    with SQLiteStore(path) as store:
        store.checkpoint(order, ts_ns=1_000)

    # Closed on exit, but the file is durable — a fresh store reads the record.
    with SQLiteStore(path) as reopened:
        loaded = reopened.get_order("0xabc")
    assert loaded is not None
    assert loaded.state is OrderState.SUBMITTED


def test_close_is_idempotent() -> None:
    store = SQLiteStore(":memory:")
    store.close()
    store.close()  # a second close is a no-op, never an error


def test_cancel_requested_marker_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "saga.db"
    order = _order()
    order.apply(_submitted())  # a marker is state-independent; SUBMITTED is fine here
    order.request_cancel(signal_id="trivial:BTC:2", ts_ns=1_500)
    with SQLiteStore(path) as store:
        store.checkpoint(order, ts_ns=2_000)

    # Recovery must see the marker so an ack-lost cancel resolves, and the cancel's
    # own signal_id so the seq high-water-mark survives the restart (ADR-0026).
    with SQLiteStore(path) as reopened:
        loaded = reopened.get_order("0xabc")
    assert loaded is not None
    assert loaded.cancel_requested is True
    assert loaded.cancel_requested_ts == 1_500
    assert loaded.cancel_signal_id == "trivial:BTC:2"


def test_default_order_round_trips_without_a_cancel_marker() -> None:
    store = SQLiteStore(":memory:")
    order = _order()
    order.apply(_submitted())
    store.checkpoint(order, ts_ns=1_000)

    loaded = store.get_order("0xabc")
    assert loaded is not None
    assert loaded.cancel_requested is False
    assert loaded.cancel_requested_ts is None
    assert loaded.cancel_signal_id is None


def test_checkpointed_order_round_trips_from_memory_store() -> None:
    store = SQLiteStore(":memory:")
    order = _order()
    order.apply(_submitted())
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
