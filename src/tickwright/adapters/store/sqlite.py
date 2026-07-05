"""``SQLiteStore`` — the default durable ``Store`` (ADR-0019).

Zero-setup, in-process, real SQL: a file for durability or ``:memory:`` for
tests, so the paper + in-memory-bus path runs and recovers with nothing
installed. One saga-record table, keyed by cloid, holding exactly what
recovery rebuilds an ``Order`` from: the order params, current state,
``cum_qty``, venue oid, terminal reason, the applied-event dedup set, and the
transition history (ADR-0008's checkpoint trail). Each checkpoint is one
transaction — the write the crash-safety argument rests on.
"""

import json
import sqlite3
import weakref
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any

from tickwright.domain import KillSwitchState, Order, OrderState, OrderType, Side

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    cloid             TEXT PRIMARY KEY,
    strategy_id       TEXT NOT NULL,
    signal_id         TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    quantity          TEXT NOT NULL,
    order_type        TEXT NOT NULL,
    state             TEXT NOT NULL,
    cum_qty           TEXT NOT NULL,
    venue_oid         TEXT,
    reason            TEXT,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    cancel_requested_ts INTEGER,
    cancel_signal_id    TEXT,
    applied_event_ids TEXT NOT NULL,
    history           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kill_switch (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    tripped  INTEGER NOT NULL,
    reason   TEXT,
    ts_ns    INTEGER NOT NULL
);
"""


class SQLiteStore:
    """A ``Store`` over one SQLite database (file path or ``":memory:"``)."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._conn = sqlite3.connect(str(path))
        # Tie the connection's lifetime to this store: close it on ``close()`` or,
        # failing that, when the store is collected — so a store that outlives its
        # explicit close (e.g. a hypothesis example) never leaks a connection.
        self._finalizer = weakref.finalize(self, self._conn.close)
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def checkpoint(self, order: Order, *, ts_ns: int) -> None:
        """Durably record ``order``'s full saga state as of ``ts_ns``.

        Upserts the record and appends ``(state, ts_ns)`` to its transition
        history, atomically.
        """
        with self._conn:
            row = self._conn.execute(
                "SELECT history FROM orders WHERE cloid = ?", (order.cloid,)
            ).fetchone()
            history: list[list[object]] = json.loads(row[0]) if row else []
            history.append([order.state.value, ts_ns])
            self._conn.execute(
                "INSERT OR REPLACE INTO orders VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order.cloid,
                    order.strategy_id,
                    order.signal_id,
                    order.symbol,
                    order.side.value,
                    str(order.quantity),
                    order.order_type.value,
                    order.state.value,
                    str(order.cum_qty),
                    order.venue_oid,
                    order.reason,
                    int(order.cancel_requested),
                    order.cancel_requested_ts,
                    order.cancel_signal_id,
                    json.dumps(sorted(order.applied_event_ids)),
                    json.dumps(history),
                ),
            )

    _RECORD_COLUMNS = (
        "cloid, strategy_id, signal_id, symbol, side, quantity, order_type,"
        " state, cum_qty, venue_oid, reason, cancel_requested,"
        " cancel_requested_ts, cancel_signal_id, applied_event_ids"
    )

    def get_order(self, cloid: str) -> Order | None:
        """Rebuild the checkpointed saga for ``cloid``, or ``None`` if unknown."""
        row = self._conn.execute(
            f"SELECT {self._RECORD_COLUMNS} FROM orders WHERE cloid = ?", (cloid,)
        ).fetchone()
        if row is None:
            return None
        return self._restore(row)

    def all_orders(self) -> list[Order]:
        """Rebuild every checkpointed saga — the recovery mass-read (ADR-0009)."""
        rows = self._conn.execute(
            f"SELECT {self._RECORD_COLUMNS} FROM orders ORDER BY cloid"
        ).fetchall()
        return [self._restore(row) for row in rows]

    @staticmethod
    def _restore(row: tuple[Any, ...]) -> Order:
        """One saga row, in ``_RECORD_COLUMNS`` order, back into an ``Order``."""
        return Order.restore(
            cloid=row[0],
            strategy_id=row[1],
            signal_id=row[2],
            symbol=row[3],
            side=Side(row[4]),
            quantity=Decimal(row[5]),
            order_type=OrderType(row[6]),
            state=OrderState(row[7]),
            cum_qty=Decimal(row[8]),
            venue_oid=row[9],
            reason=row[10],
            cancel_requested=bool(row[11]),
            cancel_requested_ts=row[12],
            cancel_signal_id=row[13],
            applied_event_ids=json.loads(row[14]),
        )

    def save_kill_switch(self, *, tripped: bool, reason: str | None, ts_ns: int) -> None:
        """Durably record the single-row kill-switch state (ADR-0026)."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO kill_switch (id, tripped, reason, ts_ns) "
                "VALUES (1, ?, ?, ?)",
                (int(tripped), reason, ts_ns),
            )

    def load_kill_switch(self) -> KillSwitchState | None:
        """The persisted kill-switch state, or ``None`` if never written."""
        row = self._conn.execute(
            "SELECT tripped, reason, ts_ns FROM kill_switch WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return KillSwitchState(tripped=bool(row[0]), reason=row[1], ts_ns=row[2])

    def history(self, cloid: str) -> list[tuple[OrderState, int]]:
        """The durable transition trail: one ``(state, ts_ns)`` per checkpoint.

        Adapter surface (audit/tests), deliberately not on the ``Store``
        Protocol — recovery rebuilds from the current record alone.
        """
        row = self._conn.execute("SELECT history FROM orders WHERE cloid = ?", (cloid,)).fetchone()
        if row is None:
            return []
        return [(OrderState(state), ts_ns) for state, ts_ns in json.loads(row[0])]

    def __enter__(self) -> "SQLiteStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection, once. A file-backed store reopens on the same path."""
        self._finalizer()
