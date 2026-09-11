"""``PostgresStore`` — the production-parity durable ``Store`` (ADR-0019).

The KafkaBus pairing's durability half: identical saga *and* ledger semantics to
``SQLiteStore`` over a real Postgres server, proven by the same store contract
suite. The same six tables — three for the saga and its neighbours, three for
the ADR-0043 accounting ledger — the same derived seq high-water (no separate
table), and no processed-event table: dedup is ``Order.apply``'s job (ADR-0025).
The members themselves are ``SqlStore``'s (``_sql``), shared with
``SQLiteStore``. What lives here is this backend's dialect: ``%s``
placeholders, the column types the DDL needs (``BIGINT``, ``BYTEA``,
``BOOLEAN``) and this driver's transaction and cursor handling.

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
from typing import Any

import psycopg

from ._sql import SqlStore

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


class PostgresStore(SqlStore):
    """A ``Store`` over one Postgres database, addressed by a libpq DSN."""

    # The one thing this adapter contributes to the seam's error contract
    # (``_durability``): the base its driver raises from.
    _driver_error = psycopg.Error
    _placeholder = "%s"

    def __init__(self, dsn: str) -> None:
        self._conn = psycopg.connect(dsn, autocommit=True)
        super().__init__(schema=_SCHEMA, release=self._conn.close)

    def _transaction(self) -> AbstractContextManager[object]:
        return self._conn.transaction()

    def _execute(self, sql: str, params: Sequence[Any] = ()) -> psycopg.Cursor[Any]:
        return self._conn.execute(sql, params)

    def _executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        # The connection has no ``executemany`` of its own; a cursor does.
        with self._conn.cursor() as cursor:
            cursor.executemany(sql, rows)
