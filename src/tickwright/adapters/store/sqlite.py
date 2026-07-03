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

from tickwright.domain import Order, OrderState, OrderType, Side

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
    applied_event_ids TEXT NOT NULL,
    history           TEXT NOT NULL
)
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
            self._conn.execute(_SCHEMA)

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
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    json.dumps(sorted(order.applied_event_ids)),
                    json.dumps(history),
                ),
            )

    def get_order(self, cloid: str) -> Order | None:
        """Rebuild the checkpointed saga for ``cloid``, or ``None`` if unknown."""
        row = self._conn.execute(
            "SELECT strategy_id, signal_id, symbol, side, quantity, order_type,"
            "       state, cum_qty, venue_oid, reason, cancel_requested,"
            "       cancel_requested_ts, applied_event_ids"
            "  FROM orders WHERE cloid = ?",
            (cloid,),
        ).fetchone()
        if row is None:
            return None
        return Order.restore(
            cloid=cloid,
            strategy_id=row[0],
            signal_id=row[1],
            symbol=row[2],
            side=Side(row[3]),
            quantity=Decimal(row[4]),
            order_type=OrderType(row[5]),
            state=OrderState(row[6]),
            cum_qty=Decimal(row[7]),
            venue_oid=row[8],
            reason=row[9],
            cancel_requested=bool(row[10]),
            cancel_requested_ts=row[11],
            applied_event_ids=json.loads(row[12]),
        )

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
