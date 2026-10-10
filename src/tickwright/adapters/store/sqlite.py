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

The members themselves are ``SqlStore``'s (``_sql``), shared with
``PostgresStore``. What lives here is this backend's dialect: ``?``
placeholders, the column types the DDL needs, and how sqlite3 scopes a
transaction.
"""

import fcntl
import os
import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager, ExitStack
from pathlib import Path
from typing import Any

from tickwright.domain import StoreLockHolder

from ._durability import durable
from ._sql import SqlStore


def _holder_of(path: str, written: str) -> StoreLockHolder:
    """The holder of the lock at ``path``, from the pid it wrote there.

    The pid can be missing for a moment, between the holder taking the lock and
    writing it. The lock still decides, so this is still a refusal.
    """
    if not written.isdigit():
        return StoreLockHolder(
            pid=None, detail=f"Another engine holds {path}. Stop it first, then try again."
        )
    return StoreLockHolder(
        pid=int(written),
        detail=f"Process {written} holds {path}. Stop that engine first, then try again.",
    )


_SCHEMA: tuple[str, ...] = (
    """
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
        acked_ts_ns       INTEGER,
        created_ts_ns     INTEGER,
        reason            TEXT,
        cancel_requested    INTEGER NOT NULL DEFAULT 0,
        cancel_requested_ts INTEGER,
        cancel_signal_id    TEXT,
        applied_event_ids TEXT NOT NULL,
        history           TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_snapshots (
        strategy_id TEXT PRIMARY KEY,
        data        BLOB NOT NULL,
        ts_ns       INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kill_switch (
        id       INTEGER PRIMARY KEY CHECK (id = 1),
        tripped  INTEGER NOT NULL,
        reason   TEXT,
        ts_ns    INTEGER NOT NULL
    )
    """,
    """
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS funding_marks (
        symbol             TEXT PRIMARY KEY,
        last_funding_ts_ns INTEGER NOT NULL,
        ts_ns              INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account (
        id                 INTEGER PRIMARY KEY CHECK (id = 1),
        account_id         TEXT NOT NULL,
        genesis_collateral TEXT NOT NULL,
        genesis_ts_ns      INTEGER NOT NULL,
        cash               TEXT NOT NULL,
        ts_ns              INTEGER NOT NULL
    )
    """,
)

# This dialect's type for each column in ``_records.ADDED_ORDER_COLUMNS``. The
# column is also in ``_SCHEMA`` for a fresh database.
_ADDED_COLUMN_TYPES: dict[str, str] = {"acked_ts_ns": "INTEGER", "created_ts_ns": "INTEGER"}


class SQLiteStore(SqlStore):
    """A ``Store`` over one SQLite database (file path or ``":memory:"``)."""

    # The one thing this adapter contributes to the seam's error contract
    # (``_durability``): the base its driver raises from.
    _driver_error = sqlite3.Error
    _placeholder = "?"

    def __init__(self, path: str | Path = ":memory:") -> None:
        conn = sqlite3.connect(str(path))
        # Holds the open lock file once ``lock()`` succeeds. Closing it is what
        # frees the lock.
        lock_file = ExitStack()

        def release() -> None:
            # The connection goes first, so no write can follow the release.
            conn.close()
            lock_file.close()

        self._conn = conn
        self._lock_file = lock_file
        super().__init__(schema=_SCHEMA, added_column_types=_ADDED_COLUMN_TYPES, release=release)

    @durable
    def lock(self) -> StoreLockHolder | None:
        # Asking the connection for its file also makes a closed store refuse.
        # The name is empty for ":memory:", which no other process can open.
        database = self._conn.execute("PRAGMA database_list").fetchone()[2]
        if not database:
            return None
        # Never the database file itself. SQLite takes its own POSIX locks there,
        # and closing any handle to it drops all of them (ADR-0052). ``flock``
        # also ends when the process dies, so a crash leaves nothing stuck.
        path = f"{database}.lock"
        file = open(path, "a+")  # stays open for as long as the lock is held
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            file.seek(0)
            written = file.read().strip()
            file.close()
            return _holder_of(path, written)
        file.truncate(0)
        file.write(str(os.getpid()))
        file.flush()
        self._lock_file.enter_context(file)
        return None

    def _has_column(self, table: str, column: str) -> bool:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return any(row[1] == column for row in rows)

    def _transaction(self) -> AbstractContextManager[object]:
        # sqlite3's connection is its own transaction scope: commit on exit,
        # roll back on raise. It does not nest (ADR-0043 §4), and no member
        # opens one inside another.
        return self._conn

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    def _executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        self._conn.executemany(sql, rows)
