"""``PostgresStore`` — the production-parity durable ``Store`` (ADR-0019).

The KafkaBus pairing's durability half: identical saga *and* ledger semantics to
``SQLiteStore`` over a real Postgres server, proven by the same store contract
suite. The same six tables — three for the saga and its neighbours, three for
the ADR-0043 accounting ledger — the same derived seq high-water (no separate
table), and no processed-event table: dedup is ``Order.apply``'s job (ADR-0025).
The row shape — the field mapping *and* the upsert semantics — is shared with
``SQLiteStore`` (``_records``); what lives here is the dialect it is rendered in
(``%s`` placeholders), the column types the DDL needs (``BIGINT``, ``BYTEA``,
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

import weakref
from collections.abc import Sequence
from types import TracebackType

import psycopg

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
    POSITION_COLUMN_LIST,
    READ_COLUMN_LIST,
    account_values,
    funding_mark_values,
    next_history,
    position_values,
    record_values,
    restore_account,
    restore_history,
    restore_order,
    restore_position,
    upserts_for,
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

# Every write this adapter makes, rendered from the shared row shape so it can
# never drift from the write tuple or from ``SQLiteStore``. ``%s`` is the whole
# of this backend's contribution.
_UPSERTS = upserts_for("%s")


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
            self._write_order(order, ts_ns=ts_ns)

    def _write_order(self, order: Order, *, ts_ns: int) -> None:
        """Upsert the saga record and append its transition entry.

        The caller owns the transaction, because ``checkpoint_ledger`` runs this
        same body inside a wider one (ADR-0043 §4). Shared rather than repeated
        so the two writes can never disagree about what a saga row is.
        """
        row = self._conn.execute(
            "SELECT history FROM orders WHERE cloid = %s", (order.cloid,)
        ).fetchone()
        history = next_history(row[0] if row else None, order.state, ts_ns)
        self._conn.execute(_UPSERTS.order, record_values(order, history=history))

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
            with self._conn.transaction(), self._conn.cursor() as cursor:
                if order is not None:
                    self._write_order(order, ts_ns=ts_ns)
                cursor.execute(_UPSERTS.account, account_values(account, ts_ns=ts_ns))
                cursor.executemany(
                    _UPSERTS.position,
                    [position_values(position, ts_ns=ts_ns) for position in positions],
                )
                if funding_mark is not None:
                    cursor.execute(
                        _UPSERTS.funding_mark, funding_mark_values(funding_mark, ts_ns=ts_ns)
                    )
        except psycopg.Error as exc:
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
            "SELECT last_funding_ts_ns FROM funding_marks WHERE symbol = %s", (symbol,)
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
