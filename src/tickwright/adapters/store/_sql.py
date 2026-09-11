"""One SQL body for both ``Store`` adapters (ADR-0019).

``SQLiteStore`` and ``PostgresStore`` hold the same six tables, write the same
rows and answer the same reads. ``_records`` fixes what a row is. This module
fixes what a member does, once, so a new member is written one time and the
two backends cannot drift on it. The contract suite still proves the parity
over a real server each; this is what makes that parity structural rather
than transcribed.

What an adapter still owns is its dialect, and nothing else: the driver's
error base (``_durability``), the parameter marker, the DDL column types, the
connection, and how that driver scopes a transaction and runs a batch write.
"""

import weakref
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager
from types import TracebackType
from typing import Any, ClassVar, Protocol, Self

from tickwright.domain import (
    Account,
    KillSwitchState,
    Order,
    OrderState,
    Position,
)

from ._durability import durable
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


class _Rows(Protocol):
    """What a driver hands back from one statement: the rows, when it has any."""

    def fetchall(self) -> Sequence[Sequence[Any]]: ...


class SqlStore(ABC):
    """The ``Store`` members, over four driver hooks an adapter fills in.

    Every member is decorated ``@durable`` here, so the seam's error contract
    (ADR-0019) holds for both backends by construction. ``close()`` is the one
    deliberate exception, for the reason ``_durability`` states.
    """

    _driver_error: ClassVar[type[Exception]]
    # The parameter marker the driver reads: ``?`` for sqlite3, ``%s`` for
    # psycopg. Every statement below renders through it.
    _placeholder: ClassVar[str]

    def __init__(self, *, schema: Iterable[str], release: Callable[[], None]) -> None:
        self._p = self._placeholder
        self._upserts = upserts_for(self._placeholder)
        # Tie the connection's lifetime to this store: close it on ``close()`` or,
        # failing that, when the store is collected — so a store that outlives its
        # explicit close (e.g. a hypothesis example) never leaks a connection.
        self._finalizer = weakref.finalize(self, release)
        with self._transaction():
            for statement in schema:
                self._execute(statement)

    @abstractmethod
    def _transaction(self) -> AbstractContextManager[object]:
        """One transaction: commit on exit, roll back on raise."""

    @abstractmethod
    def _execute(self, sql: str, params: Sequence[Any] = ()) -> _Rows:
        """Run one statement, inside the open transaction if there is one."""

    @abstractmethod
    def _executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        """Run one write once per row, inside the open transaction."""

    def _one(self, sql: str, params: Sequence[Any] = ()) -> Sequence[Any] | None:
        rows = self._execute(sql, params).fetchall()
        return rows[0] if rows else None

    @durable
    def checkpoint(self, order: Order, *, ts_ns: int) -> None:
        """Durably record ``order``'s full saga state as of ``ts_ns``.

        Upserts the record and appends ``(state, ts_ns)`` to its transition
        history, atomically — one transaction per checkpoint.
        """
        with self._transaction():
            self._write_order(order, ts_ns=ts_ns)

    def _write_order(self, order: Order, *, ts_ns: int) -> None:
        """Upsert the saga record and append its transition entry.

        The caller owns the transaction, because ``checkpoint_ledger`` runs this
        same body inside a wider one (ADR-0043 §4). Shared rather than repeated
        so the two writes can never disagree about what a saga row is.
        """
        row = self._one(f"SELECT history FROM orders WHERE cloid = {self._p}", (order.cloid,))
        history = next_history(row[0] if row else None, order.state, ts_ns)
        self._execute(self._upserts.order, record_values(order, history=history))

    @durable
    def get_order(self, cloid: str) -> Order | None:
        """Rebuild the checkpointed saga for ``cloid``, or ``None`` if unknown."""
        row = self._one(f"SELECT {READ_COLUMN_LIST} FROM orders WHERE cloid = {self._p}", (cloid,))
        if row is None:
            return None
        return restore_order(row)

    @durable
    def all_orders(self) -> list[Order]:
        """Rebuild every checkpointed saga — the recovery mass-read (ADR-0009)."""
        rows = self._execute(f"SELECT {READ_COLUMN_LIST} FROM orders ORDER BY cloid").fetchall()
        return [restore_order(row) for row in rows]

    @durable
    def save_strategy_snapshot(self, strategy_id: str, data: bytes, *, ts_ns: int) -> None:
        """Durably record ``strategy_id``'s opaque state bytes; latest wins (ADR-0016)."""
        with self._transaction():
            self._execute(self._upserts.snapshot, (strategy_id, data, ts_ns))

    @durable
    def load_strategy_snapshot(self, strategy_id: str) -> bytes | None:
        """The last persisted snapshot for ``strategy_id``, or ``None`` if never saved."""
        row = self._one(
            f"SELECT data FROM strategy_snapshots WHERE strategy_id = {self._p}", (strategy_id,)
        )
        return None if row is None else bytes(row[0])

    @durable
    def save_kill_switch(self, *, tripped: bool, reason: str | None, ts_ns: int) -> None:
        """Durably record the single-row kill-switch state (ADR-0026).

        ``tripped`` goes over as the ``bool`` it is. Postgres stores it in a
        ``BOOLEAN`` column; sqlite3 adapts a ``bool`` to its ``INTEGER``, and
        ``load_kill_switch`` reads both back through ``bool``.
        """
        with self._transaction():
            self._execute(self._upserts.kill_switch, (tripped, reason, ts_ns))

    @durable
    def load_kill_switch(self) -> KillSwitchState | None:
        """The persisted kill-switch state, or ``None`` if never written."""
        row = self._one("SELECT tripped, reason, ts_ns FROM kill_switch WHERE id = 1")
        if row is None:
            return None
        return KillSwitchState(tripped=bool(row[0]), reason=row[1], ts_ns=row[2])

    @durable
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
        run on believing the ledger moved (ADR-0014). That translation is the
        seam's, not this method's (``_durability``): it was the one member that
        made the promise, and now every member does.
        """
        with self._transaction():
            if order is not None:
                self._write_order(order, ts_ns=ts_ns)
            self._execute(self._upserts.account, account_values(account, ts_ns=ts_ns))
            self._executemany(
                self._upserts.position,
                [position_values(position, ts_ns=ts_ns) for position in positions],
            )
            if funding_mark is not None:
                self._execute(
                    self._upserts.funding_mark, funding_mark_values(funding_mark, ts_ns=ts_ns)
                )

    @durable
    def all_positions(self) -> list[Position]:
        """Every persisted partition — the recovery mass-read (ADR-0043 §9)."""
        rows = self._execute(
            f"SELECT {POSITION_COLUMN_LIST} FROM positions ORDER BY strategy_id, symbol"
        ).fetchall()
        return [restore_position(row) for row in rows]

    @durable
    def has_orders(self) -> bool:
        """Whether any saga history exists at all — the existence question the
        startup refusal asks before ``cache.rebuild()`` (ADR-0043 §9). Answering
        it with ``all_orders()`` would deserialize every saga in the store twice
        on every start, on the recovery path."""
        return self._one("SELECT 1 FROM orders LIMIT 1") is not None

    @durable
    def funding_mark(self, symbol: str) -> int | None:
        """The last funding boundary applied to ``symbol``, or ``None`` if none
        ever was — the "never accrued" state ADR-0043 §3 encodes as row absence,
        which admits any boundary since nothing has been applied to contradict
        it."""
        row = self._one(
            f"SELECT last_funding_ts_ns FROM funding_marks WHERE symbol = {self._p}", (symbol,)
        )
        return None if row is None else int(row[0])

    @durable
    def load_account(self) -> Account | None:
        """The persisted account, or ``None`` if the ledger was never opened."""
        row = self._one(f"SELECT {ACCOUNT_COLUMN_LIST} FROM account WHERE id = 1")
        return None if row is None else restore_account(row)

    @durable
    def history(self, cloid: str) -> list[tuple[OrderState, int]]:
        """The durable transition trail: one ``(state, ts_ns)`` per checkpoint.

        On the ``Store`` Protocol as the seam's audit surface: recovery rebuilds
        from the current record alone, but the engine's tests read the trail to
        assert what a saga did, so it is part of what an implementation must
        provide.
        """
        row = self._one(f"SELECT history FROM orders WHERE cloid = {self._p}", (cloid,))
        return restore_history(row[0] if row else None)

    def __enter__(self) -> Self:
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
