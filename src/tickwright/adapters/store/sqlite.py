"""``SQLiteStore`` — the default durable ``Store`` (ADR-0019).

Zero-setup, in-process, real SQL: a file for durability or ``:memory:`` for
tests, so the paper + in-memory-bus path runs and recovers with nothing
installed.

The saga-record table is keyed by cloid and holds exactly what recovery rebuilds
an ``Order`` from: the order params, current state, ``cum_qty``, venue oid,
terminal reason, the applied-event dedup set, and the transition history
(ADR-0008's checkpoint trail). Alongside it the accounting ledger (ADR-0043):
``positions`` per ``(strategy, symbol)``, the single-row ``account``, and the
``funding_marks`` watermark. Ledger rows are current state upserted in place,
not an event log.

Each checkpoint is one transaction — the write the crash-safety argument rests
on — and ``checkpoint_ledger`` widens that to one transaction across the order
row and the ledger together, because a fill moves both.
"""

import sqlite3
import weakref
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType

from tickwright.domain import (
    Account,
    InvariantViolation,
    KillSwitchState,
    Order,
    OrderState,
    Position,
)

from ._records import (
    ACCOUNT_COLUMN_LIST,
    ACCOUNT_COLUMNS,
    ACCOUNT_UPDATE_COLUMNS,
    FUNDING_MARK_COLUMNS,
    FUNDING_MARK_UPDATE_COLUMNS,
    POSITION_COLUMN_LIST,
    POSITION_COLUMNS,
    POSITION_KEY_COLUMNS,
    POSITION_UPDATE_COLUMNS,
    READ_COLUMN_LIST,
    RECORD_COLUMNS,
    account_values,
    funding_mark_values,
    next_history,
    position_values,
    record_values,
    restore_account,
    restore_history,
    restore_order,
    restore_position,
)

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
CREATE TABLE IF NOT EXISTS strategy_snapshots (
    strategy_id TEXT PRIMARY KEY,
    data        BLOB NOT NULL,
    ts_ns       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kill_switch (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    tripped  INTEGER NOT NULL,
    reason   TEXT,
    ts_ns    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    strategy_id         TEXT NOT NULL,
    symbol              TEXT NOT NULL,
    signed_size         TEXT NOT NULL,
    entry_price         TEXT,
    realized_pnl        TEXT NOT NULL,
    fees                TEXT NOT NULL,
    funding             TEXT NOT NULL,
    isolated_collateral TEXT,
    ts_ns               INTEGER NOT NULL,
    PRIMARY KEY (strategy_id, symbol)
);
CREATE TABLE IF NOT EXISTS funding_marks (
    symbol             TEXT PRIMARY KEY,
    last_funding_ts_ns INTEGER NOT NULL,
    ts_ns              INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS account (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    account_id         TEXT NOT NULL,
    genesis_collateral TEXT NOT NULL,
    genesis_ts_ns      INTEGER NOT NULL,
    cash               TEXT NOT NULL,
    ts_ns              INTEGER NOT NULL
);
"""

# The account upsert, built from the shared column list so it cannot drift from
# the write tuple. Deliberately not ``INSERT OR REPLACE`` like the saga row: a
# replace rewrites every column, and the write-once trio (ADR-0043 §3) must
# survive an upsert, so only ``ACCOUNT_UPDATE_COLUMNS`` are overwritten.
_UPSERT_ACCOUNT = (
    f"INSERT INTO account (id, {ACCOUNT_COLUMN_LIST}) "
    f"VALUES (1, {', '.join('?' * len(ACCOUNT_COLUMNS))}) "
    "ON CONFLICT (id) DO UPDATE SET "
    + ", ".join(f"{column} = excluded.{column}" for column in ACCOUNT_UPDATE_COLUMNS)
)

_UPSERT_POSITION = (
    f"INSERT INTO positions ({POSITION_COLUMN_LIST}) "
    f"VALUES ({', '.join('?' * len(POSITION_COLUMNS))}) "
    f"ON CONFLICT ({', '.join(POSITION_KEY_COLUMNS)}) DO UPDATE SET "
    + ", ".join(f"{column} = excluded.{column}" for column in POSITION_UPDATE_COLUMNS)
)

_UPSERT_FUNDING_MARK = (
    f"INSERT INTO funding_marks ({', '.join(FUNDING_MARK_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(FUNDING_MARK_COLUMNS))}) "
    "ON CONFLICT (symbol) DO UPDATE SET "
    + ", ".join(f"{column} = excluded.{column}" for column in FUNDING_MARK_UPDATE_COLUMNS)
)


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
            self._write_order(order, ts_ns=ts_ns)

    def _write_order(self, order: Order, *, ts_ns: int) -> None:
        """Upsert the saga record and append its transition entry.

        The caller owns the transaction, because ``checkpoint_ledger`` runs this
        same body inside a wider one (ADR-0043 §4). Shared rather than repeated
        so the two writes can never disagree about what a saga row is.
        """
        row = self._conn.execute(
            "SELECT history FROM orders WHERE cloid = ?", (order.cloid,)
        ).fetchone()
        history = next_history(row[0] if row else None, order.state, ts_ns)
        placeholders = ", ".join("?" * len(RECORD_COLUMNS))
        self._conn.execute(
            f"INSERT OR REPLACE INTO orders VALUES ({placeholders})",
            record_values(order, history=history),
        )

    def get_order(self, cloid: str) -> Order | None:
        """Rebuild the checkpointed saga for ``cloid``, or ``None`` if unknown."""
        row = self._conn.execute(
            f"SELECT {READ_COLUMN_LIST} FROM orders WHERE cloid = ?", (cloid,)
        ).fetchone()
        if row is None:
            return None
        return restore_order(row)

    def all_orders(self) -> list[Order]:
        """Rebuild every checkpointed saga — the recovery mass-read (ADR-0009)."""
        rows = self._conn.execute(
            f"SELECT {READ_COLUMN_LIST} FROM orders ORDER BY cloid"
        ).fetchall()
        return [restore_order(row) for row in rows]

    def save_strategy_snapshot(self, strategy_id: str, data: bytes, *, ts_ns: int) -> None:
        """Durably record ``strategy_id``'s opaque state bytes; latest wins (ADR-0016)."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO strategy_snapshots (strategy_id, data, ts_ns) "
                "VALUES (?, ?, ?)",
                (strategy_id, data, ts_ns),
            )

    def load_strategy_snapshot(self, strategy_id: str) -> bytes | None:
        """The last persisted snapshot for ``strategy_id``, or ``None`` if never saved."""
        row = self._conn.execute(
            "SELECT data FROM strategy_snapshots WHERE strategy_id = ?", (strategy_id,)
        ).fetchone()
        return None if row is None else bytes(row[0])

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

    def checkpoint_ledger(
        self,
        *,
        account: Account,
        positions: Sequence[Position] = (),
        order: Order | None = None,
        funding_mark: tuple[str, int] | None = None,
        ts_ns: int,
    ) -> None:
        """Durably record the ledger as of ``ts_ns`` — one transaction (ADR-0043 §4).

        The order row, the position rows and the account row commit together or
        not at all: as two transactions either ordering is unsound, and on paper
        the resulting half-fill never heals, because the in-process venue holds
        no position state and this store is the ledger's sole authority.

        A write the backend refuses raises ``InvariantViolation`` — the
        transaction has already rolled back, so what the caller must not do is
        run on believing the ledger moved (ADR-0014).
        """
        try:
            with self._conn:
                if order is not None:
                    self._write_order(order, ts_ns=ts_ns)
                self._conn.execute(_UPSERT_ACCOUNT, account_values(account, ts_ns=ts_ns))
                self._conn.executemany(
                    _UPSERT_POSITION,
                    [position_values(position, ts_ns=ts_ns) for position in positions],
                )
                if funding_mark is not None:
                    self._conn.execute(
                        _UPSERT_FUNDING_MARK, funding_mark_values(funding_mark, ts_ns=ts_ns)
                    )
        except sqlite3.Error as exc:
            raise InvariantViolation(f"ledger checkpoint at ts_ns={ts_ns} refused: {exc}") from exc

    def all_positions(self) -> list[Position]:
        """Every persisted partition — the recovery mass-read (ADR-0043 §9)."""
        rows = self._conn.execute(
            f"SELECT {POSITION_COLUMN_LIST} FROM positions ORDER BY strategy_id, symbol"
        ).fetchall()
        return [restore_position(row) for row in rows]

    def has_orders(self) -> bool:
        """Whether any saga history exists at all — the existence question the
        startup refusal asks before ``cache.rebuild()`` (ADR-0043 §9). Answering
        it with ``all_orders()`` would deserialize every saga in the store twice
        on every start, on the recovery path."""
        return self._conn.execute("SELECT 1 FROM orders LIMIT 1").fetchone() is not None

    def funding_mark(self, symbol: str) -> int | None:
        """The last funding boundary applied to ``symbol``, or ``None`` if none
        ever was — the "never accrued" state ADR-0043 §3 encodes as row absence,
        which admits any boundary since nothing has been applied to contradict
        it."""
        row = self._conn.execute(
            "SELECT last_funding_ts_ns FROM funding_marks WHERE symbol = ?", (symbol,)
        ).fetchone()
        return None if row is None else int(row[0])

    def load_account(self) -> Account | None:
        """The persisted account, or ``None`` if the ledger was never opened."""
        row = self._conn.execute(
            f"SELECT {ACCOUNT_COLUMN_LIST} FROM account WHERE id = 1"
        ).fetchone()
        return None if row is None else restore_account(row)

    def history(self, cloid: str) -> list[tuple[OrderState, int]]:
        """The durable transition trail: one ``(state, ts_ns)`` per checkpoint.

        Adapter surface (audit/tests), deliberately not on the ``Store``
        Protocol — recovery rebuilds from the current record alone.
        """
        row = self._conn.execute("SELECT history FROM orders WHERE cloid = ?", (cloid,)).fetchone()
        return restore_history(row[0] if row else None)

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
