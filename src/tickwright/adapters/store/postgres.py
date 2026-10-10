"""``PostgresStore`` — the production-parity durable ``Store`` (ADR-0019).

The KafkaBus pairing's durability half: identical saga *and* ledger semantics to
``SQLiteStore`` over a real Postgres server, proven by the same store contract
suite. The same six tables — three for the saga and its neighbours, three for
the ADR-0043 accounting ledger — the same derived seq high-water (no separate
table), and no processed-event table: dedup is ``Order.apply``'s job (ADR-0025).
The members themselves are ``SqlStore``'s (``_sql``), shared with
``SQLiteStore``. What lives here is this backend's dialect: ``%s``
placeholders, the column types the DDL needs (``BIGINT``, ``BYTEA``,
``BOOLEAN``) and this driver's transaction and cursor handling. The one member
written here is ``lock()``. It is an advisory lock on the write connection
(ADR-0052).

Money stays ``TEXT`` here rather than ``NUMERIC`` (ADR-0043 §7). ``NUMERIC``
normalises representation — ``1000`` for ``1E+3``, ``0.00000000`` for ``0E-8`` —
so it would fork the value mapping per backend, which is precisely what
``_records`` exists to prevent. The cost is no bare ``SELECT SUM(realized_pnl)``;
nothing in the engine needs SQL-side arithmetic, and the cast is standard.

The connection is synchronous and each checkpoint is one ``conn.transaction()``
block — the atomic write the crash-safety argument rests on (ADR-0008), widened
by ``checkpoint_ledger`` to cover the order row and the ledger together. The
connection runs in autocommit mode so reads never leave a transaction idle open;
the explicit transaction blocks wrap exactly the read-modify-write checkpoints.
"""

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

import psycopg

from tickwright.domain import StoreLockHolder

from ._durability import durable
from ._sql import SqlStore

# The store lock's advisory key. An advisory lock is per database, so two stores
# on one database are one store. The key is below 2**31, so ``pg_locks`` shows
# it whole in ``objid`` with ``classid`` 0.
_LOCK_KEY = 0x7477_6C6B  # "twlk"

# The session that holds the store lock, if any still does.
_HOLDER_QUERY = """
    SELECT a.pid, a.client_addr, a.backend_start
    FROM pg_locks AS l JOIN pg_stat_activity AS a ON a.pid = l.pid
    WHERE l.locktype = 'advisory' AND l.granted
      AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND l.classid = 0 AND l.objid::bigint = %s AND l.objsubid = 1
"""


def _holder_of(store: str, pid: int, client: object, started: datetime | None) -> StoreLockHolder:
    """The refusal the operator reads. It says how to end the holder (ADR-0052)."""
    source = "a local socket" if client is None else str(client)
    return StoreLockHolder(
        pid=pid,
        detail=(
            f"Postgres session {pid} from {source} holds this store's lock. "
            f"It started at {started}. If that engine is dead, the server frees the lock "
            f"once it notices. To end it now, run SELECT pg_terminate_backend({pid})."
        ),
        store=store,
    )


# Individual DDL statements: psycopg's extended protocol runs one command per
# ``execute``, so the schema is applied statement by statement.
_SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS orders (
        cloid               TEXT PRIMARY KEY,
        strategy_id         TEXT NOT NULL,
        signal_id           TEXT NOT NULL,
        symbol              TEXT NOT NULL,
        side                TEXT NOT NULL,
        quantity            TEXT NOT NULL,
        order_type          TEXT NOT NULL,
        state               TEXT NOT NULL,
        cum_qty             TEXT NOT NULL,
        venue_oid           TEXT,
        acked_ts_ns         BIGINT,
        created_ts_ns       BIGINT,
        reason              TEXT,
        cancel_requested    BOOLEAN NOT NULL DEFAULT FALSE,
        cancel_requested_ts BIGINT,
        cancel_signal_id    TEXT,
        applied_event_ids   TEXT NOT NULL,
        history             TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS strategy_snapshots (
        strategy_id TEXT PRIMARY KEY,
        data        BYTEA NOT NULL,
        ts_ns       BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS kill_switch (
        id      INTEGER PRIMARY KEY CHECK (id = 1),
        tripped BOOLEAN NOT NULL,
        reason  TEXT,
        ts_ns   BIGINT NOT NULL
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
        ts_ns               BIGINT NOT NULL,
        PRIMARY KEY (strategy_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS funding_marks (
        symbol             TEXT PRIMARY KEY,
        last_funding_ts_ns BIGINT NOT NULL,
        ts_ns              BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS account (
        id                 INTEGER PRIMARY KEY CHECK (id = 1),
        account_id         TEXT NOT NULL,
        genesis_collateral TEXT NOT NULL,
        genesis_ts_ns      BIGINT NOT NULL,
        cash               TEXT NOT NULL,
        ts_ns              BIGINT NOT NULL
    )
    """,
)

# This dialect's type for each column in ``_records.ADDED_ORDER_COLUMNS``. The
# column is also in ``_SCHEMA`` for a fresh database.
_ADDED_COLUMN_TYPES: dict[str, str] = {"acked_ts_ns": "BIGINT", "created_ts_ns": "BIGINT"}


class PostgresStore(SqlStore):
    """A ``Store`` over one Postgres database, addressed by a libpq DSN."""

    # The one thing this adapter contributes to the seam's error contract
    # (``_durability``): the base its driver raises from.
    _driver_error = psycopg.Error
    _placeholder = "%s"

    def __init__(self, dsn: str) -> None:
        self._conn = psycopg.connect(dsn, autocommit=True)
        super().__init__(
            schema=_SCHEMA, added_column_types=_ADDED_COLUMN_TYPES, release=self._conn.close
        )

    @durable
    def lock(self) -> StoreLockHolder | None:
        # On the connection the store writes through, never a second one. A lost
        # session then fails the next write, so the engine cannot keep trading
        # without its lock (ADR-0052).
        while True:
            row = self._conn.execute("SELECT pg_try_advisory_lock(%s)", (_LOCK_KEY,)).fetchone()
            if row is not None and row[0]:
                return None
            holder = self._conn.execute(_HOLDER_QUERY, (_LOCK_KEY,)).fetchone()
            if holder is not None:
                # Built from the live connection, which never shows the password.
                info = self._conn.info
                return _holder_of(f"{info.host}:{info.port}/{info.dbname}", *holder)
            # The holder let go between the two reads, so the lock may be free.

    def _has_column(self, table: str, column: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
            (table, column),
        ).fetchone()
        return row is not None

    def _transaction(self) -> AbstractContextManager[object]:
        return self._conn.transaction()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> psycopg.Cursor[Any]:
        return self._conn.execute(sql, params)

    def _executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        # The connection has no ``executemany`` of its own; a cursor does.
        with self._conn.cursor() as cursor:
            cursor.executemany(sql, rows)
