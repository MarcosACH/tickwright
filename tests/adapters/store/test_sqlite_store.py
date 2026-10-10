"""SQLite-specific ``Store`` behavior (ADR-0019).

The cross-backend contract — round-trips, history, dedup, durability across a
reopen, upserts, kill-switch — lives in ``test_store_contract.py`` and runs
against both adapters. What remains here is the one thing only ``SQLiteStore``
has: the ``:memory:`` default, the zero-setup in-process store the hermetic
paper + in-memory-bus path recovers from with nothing installed.
"""

import errno
import fcntl
import gc
import os
import warnings
from decimal import Decimal
from pathlib import Path

import pytest

from tickwright.adapters.store import SQLiteStore
from tickwright.domain import Order, OrderState, OrderSubmitted, OrderType, Side


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


def test_in_memory_default_round_trips_a_checkpoint_within_the_session() -> None:
    store = SQLiteStore()  # ":memory:" — the zero-setup default
    order = _order()
    order.apply(_submitted())
    store.checkpoint(order, ts_ns=1_000)

    loaded = store.get_order("0xabc")
    assert loaded is not None
    assert loaded.state is OrderState.SUBMITTED
    store.close()


def test_a_held_lock_names_the_holder_pid_and_the_lock_file(tmp_path: Path) -> None:
    """The lock is an OS lock on ``<db>.lock``, never on the database file
    (ADR-0052). The pid is written there only so the refusal can name it."""
    db = tmp_path / "tickwright.db"
    with SQLiteStore(db) as first, SQLiteStore(db) as second:
        assert first.lock() is None

        holder = second.lock()

        assert holder is not None
        assert holder.pid == os.getpid()
        assert str(tmp_path / "tickwright.db.lock") in holder.detail


def test_an_in_memory_store_always_gets_the_lock() -> None:
    """No other process can open a ``:memory:`` database, so nothing can contend."""
    with SQLiteStore() as store:
        assert store.lock() is None


def test_a_failed_flock_leaves_no_lock_file_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Some filesystems refuse ``flock`` outright, for example with ``ENOLCK`` on
    NFS. The engine faults on that error, and the file it opened must not stay
    open behind it. The OS call is the process boundary, so it is the one faked."""

    def refuse(file: object, operation: int) -> None:
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", refuse)
    with SQLiteStore(tmp_path / "tickwright.db") as store:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            try:
                store.lock()
            except OSError as exc:
                assert exc.errno == errno.ENOLCK
            else:
                raise AssertionError("lock() must not succeed when flock fails")
            gc.collect()

    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]
