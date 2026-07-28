"""``PostgresStore`` — the production-parity durable ``Store`` (ADR-0019).

The KafkaBus pairing's durability half: identical saga semantics to
``SQLiteStore`` over a real Postgres server, proven by the same store contract
suite. Same three tables, the same derived seq high-water (no separate table),
and no processed-event table — dedup is ``Order.apply``'s job (ADR-0025). The
field mapping is shared with ``SQLiteStore`` (``_records``); only the SQL
dialect — ``%s`` placeholders and ``ON CONFLICT`` upserts — lives here.

The connection is synchronous and each checkpoint is one ``conn.transaction()``
block — the atomic write the crash-safety argument rests on (ADR-0008). The
connection runs in autocommit mode so reads never leave a transaction idle open;
the explicit transaction blocks wrap exactly the read-modify-write checkpoints.
"""

import weakref
from collections.abc import Sequence
from types import TracebackType

import psycopg

from tickwright.domain import Account, KillSwitchState, Order, OrderState, Position

from ._records import (
    ACCOUNT_COLUMN_LIST,
    ACCOUNT_COLUMNS,
    ACCOUNT_UPDATE_COLUMNS,
    POSITION_COLUMN_LIST,
    POSITION_COLUMNS,
    POSITION_KEY_COLUMNS,
    POSITION_UPDATE_COLUMNS,
    READ_COLUMN_LIST,
    RECORD_COLUMNS,
    account_values,
    next_history,
    position_values,
    record_values,
    restore_account,
    restore_history,
    restore_order,
    restore_position,
)

# Individual DDL statements: psycopg's extended protocol runs one command per
# ``execute``, so the schema is applied statement by statement.
_SCHEMA_STATEMENTS: tuple[str, ...] = (
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

# Upsert built from the shared column list so it can never drift from the write
# tuple: insert every column, and on a cloid collision overwrite each non-key
# column with the incoming value.
_UPSERT_ORDER = (
    f"INSERT INTO orders ({', '.join(RECORD_COLUMNS)}) "
    f"VALUES ({', '.join(['%s'] * len(RECORD_COLUMNS))}) "
    "ON CONFLICT (cloid) DO UPDATE SET "
    + ", ".join(f"{column} = EXCLUDED.{column}" for column in RECORD_COLUMNS if column != "cloid")
)

# The account upsert. Only ``ACCOUNT_UPDATE_COLUMNS`` are overwritten: the
# write-once trio (ADR-0043 §3) is inserted with the row and never moved.
_UPSERT_ACCOUNT = (
    f"INSERT INTO account (id, {ACCOUNT_COLUMN_LIST}) "
    f"VALUES (1, {', '.join(['%s'] * len(ACCOUNT_COLUMNS))}) "
    "ON CONFLICT (id) DO UPDATE SET "
    + ", ".join(f"{column} = EXCLUDED.{column}" for column in ACCOUNT_UPDATE_COLUMNS)
)

_UPSERT_POSITION = (
    f"INSERT INTO positions ({POSITION_COLUMN_LIST}) "
    f"VALUES ({', '.join(['%s'] * len(POSITION_COLUMNS))}) "
    f"ON CONFLICT ({', '.join(POSITION_KEY_COLUMNS)}) DO UPDATE SET "
    + ", ".join(f"{column} = EXCLUDED.{column}" for column in POSITION_UPDATE_COLUMNS)
)


class PostgresStore:
    """A ``Store`` over one Postgres database, addressed by a libpq DSN."""

    def __init__(self, dsn: str) -> None:
        self._conn = psycopg.connect(dsn, autocommit=True)
        # Tie the connection's lifetime to this store: close it on ``close()`` or,
        # failing that, when the store is collected — so a store that outlives its
        # explicit close never leaks a connection (which the suite treats as a
        # failure via the ``ResourceWarning`` gate).
        self._finalizer = weakref.finalize(self, self._conn.close)
        with self._conn.transaction():
            for statement in _SCHEMA_STATEMENTS:
                self._conn.execute(statement)

    def checkpoint(self, order: Order, *, ts_ns: int) -> None:
        """Durably record ``order``'s full saga state as of ``ts_ns``.

        Upserts the record and appends ``(state, ts_ns)`` to its transition
        history, atomically — one transaction per checkpoint.
        """
        with self._conn.transaction():
            row = self._conn.execute(
                "SELECT history FROM orders WHERE cloid = %s", (order.cloid,)
            ).fetchone()
            history = next_history(row[0] if row else None, order.state, ts_ns)
            self._conn.execute(_UPSERT_ORDER, record_values(order, history=history))

    def get_order(self, cloid: str) -> Order | None:
        """Rebuild the checkpointed saga for ``cloid``, or ``None`` if unknown."""
        row = self._conn.execute(
            f"SELECT {READ_COLUMN_LIST} FROM orders WHERE cloid = %s", (cloid,)
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
        with self._conn.transaction():
            self._conn.execute(
                "INSERT INTO strategy_snapshots (strategy_id, data, ts_ns) "
                "VALUES (%s, %s, %s) "
                "ON CONFLICT (strategy_id) DO UPDATE SET "
                "data = EXCLUDED.data, ts_ns = EXCLUDED.ts_ns",
                (strategy_id, data, ts_ns),
            )

    def load_strategy_snapshot(self, strategy_id: str) -> bytes | None:
        """The last persisted snapshot for ``strategy_id``, or ``None`` if never saved."""
        row = self._conn.execute(
            "SELECT data FROM strategy_snapshots WHERE strategy_id = %s", (strategy_id,)
        ).fetchone()
        return None if row is None else bytes(row[0])

    def save_kill_switch(self, *, tripped: bool, reason: str | None, ts_ns: int) -> None:
        """Durably record the single-row kill-switch state (ADR-0026)."""
        with self._conn.transaction():
            self._conn.execute(
                "INSERT INTO kill_switch (id, tripped, reason, ts_ns) "
                "VALUES (1, %s, %s, %s) "
                "ON CONFLICT (id) DO UPDATE SET "
                "tripped = EXCLUDED.tripped, reason = EXCLUDED.reason, ts_ns = EXCLUDED.ts_ns",
                (tripped, reason, ts_ns),
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
        ts_ns: int,
    ) -> None:
        """Durably record the ledger as of ``ts_ns`` — one transaction (ADR-0043 §4)."""
        with self._conn.transaction(), self._conn.cursor() as cursor:
            cursor.execute(_UPSERT_ACCOUNT, account_values(account, ts_ns=ts_ns))
            cursor.executemany(
                _UPSERT_POSITION,
                [position_values(position, ts_ns=ts_ns) for position in positions],
            )

    def all_positions(self) -> list[Position]:
        """Every persisted partition — the recovery mass-read (ADR-0043 §9)."""
        rows = self._conn.execute(
            f"SELECT {POSITION_COLUMN_LIST} FROM positions ORDER BY strategy_id, symbol"
        ).fetchall()
        return [restore_position(row) for row in rows]

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
        row = self._conn.execute("SELECT history FROM orders WHERE cloid = %s", (cloid,)).fetchone()
        return restore_history(row[0] if row else None)

    def __enter__(self) -> "PostgresStore":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection, once. A store reopened on the same DSN reads the
        durable record — every checkpoint already committed, so closing loses
        nothing. Idempotent: the finalizer runs the close exactly once."""
        self._finalizer()
