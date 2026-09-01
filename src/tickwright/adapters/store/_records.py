"""Shared row shape for the ``Store`` adapters (ADR-0019).

Both ``SQLiteStore`` and ``PostgresStore`` persist the same rows: the same
columns, the same JSON encoding of the applied-event dedup set and the
transition history, the same ``Decimal``-as-``TEXT`` money mapping, and the same
upsert semantics. This module owns all of it, so the two backends cannot drift
on what a row *is* or on how it is written; each backend keeps only what is
genuinely per-dialect — the parameter marker, and the DDL's column types.

Money is written ``str(value)`` and read ``Decimal(text)``, exact in
*representation* rather than merely in numeric value: trailing zeros, ``-0`` and
exponent forms all survive, because ``str(Decimal)`` preserves coefficient and
exponent (ADR-0043 §7).
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any

from tickwright.domain import (
    UNATTRIBUTED,
    Account,
    Order,
    OrderState,
    OrderType,
    Position,
    Side,
)

_ZERO = Decimal("0")

# The saga columns, in write order. ``history`` (the ADR-0008 checkpoint trail)
# is last: it is an audit surface, not part of what recovery reads.
RECORD_COLUMNS: tuple[str, ...] = (
    "cloid",
    "strategy_id",
    "signal_id",
    "symbol",
    "side",
    "quantity",
    "order_type",
    "state",
    "cum_qty",
    "venue_oid",
    "reason",
    "cancel_requested",
    "cancel_requested_ts",
    "cancel_signal_id",
    "applied_event_ids",
    "history",
)

RECORD_KEY_COLUMNS: tuple[str, ...] = ("cloid",)

# What an upsert may move: every column but the key it collided on. A saga row
# has no write-once trio — recovery rebuilds from the current record, so every
# non-key column follows the incoming order.
RECORD_UPDATE_COLUMNS: tuple[str, ...] = tuple(
    column for column in RECORD_COLUMNS if column not in RECORD_KEY_COLUMNS
)

# What ``get_order`` / ``all_orders`` select to rebuild an ``Order`` — every
# column but ``history``, which recovery does not consult.
READ_COLUMNS: tuple[str, ...] = RECORD_COLUMNS[:-1]

READ_COLUMN_LIST = ", ".join(READ_COLUMNS)


def record_values(order: Order, *, history: Sequence[Any]) -> tuple[Any, ...]:
    """The full write tuple for ``order``, in ``RECORD_COLUMNS`` order.

    Decimals and enums serialize to text; the dedup set and ``history`` to JSON —
    the exact shape both backends store, so a saga round-trips identically
    whichever one holds it.
    """
    return (
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
        order.cancel_requested,
        order.cancel_requested_ts,
        order.cancel_signal_id,
        json.dumps(sorted(order.applied_event_ids)),
        json.dumps(list(history)),
    )


def next_history(existing_json: str | None, state: OrderState, ts_ns: int) -> list[list[Any]]:
    """The transition trail with ``(state, ts_ns)`` appended (ADR-0008)."""
    history: list[list[Any]] = json.loads(existing_json) if existing_json else []
    history.append([state.value, ts_ns])
    return history


def restore_order(row: Sequence[Any]) -> Order:
    """One saga row, in ``READ_COLUMNS`` order, back into an ``Order``.

    ``cancel_requested`` is read through ``bool`` so a backend that stores it as
    an integer (SQLite) and one that stores it as a native boolean (Postgres)
    both restore the same marker.
    """
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


def restore_history(history_json: str | None) -> list[tuple[OrderState, int]]:
    """The durable ``(state, ts_ns)`` trail, decoded — the audit read."""
    if not history_json:
        return []
    return [(OrderState(state), ts_ns) for state, ts_ns in json.loads(history_json)]


# The account row, in write order — the single row ADR-0043 §3 pins with
# ``CHECK (id = 1)``, so ``id`` is a literal in the SQL rather than a value here.
# ``account_id``, ``genesis_collateral`` and ``genesis_ts_ns`` lead because they
# are the write-once trio: both backends insert them and exclude them from the
# upsert's update list, which is what "written once, with the row" means in DDL.
ACCOUNT_COLUMNS: tuple[str, ...] = (
    "account_id",
    "genesis_collateral",
    "genesis_ts_ns",
    "cash",
    "ts_ns",
)

ACCOUNT_COLUMN_LIST = ", ".join(ACCOUNT_COLUMNS)

# The key is the constraint itself: ``CHECK (id = 1)`` is what makes the account
# one row (ADR-0043 §3), so ``id`` is a literal in the statement rather than a
# column any caller writes.
ACCOUNT_KEY_COLUMNS: tuple[str, ...] = ("id",)

# What an upsert may move: everything but the write-once trio and the key.
ACCOUNT_UPDATE_COLUMNS: tuple[str, ...] = ("cash", "ts_ns")


def account_values(account: Account, *, ts_ns: int) -> tuple[Any, ...]:
    """The account write tuple, in ``ACCOUNT_COLUMNS`` order."""
    return (
        account.account_id,
        str(account.genesis_collateral),
        account.genesis_ts_ns,
        str(account.cash),
        ts_ns,
    )


# ``UNATTRIBUTED`` is the on-disk stand-in for ADR-0038's ``strategy_id: None``,
# translated here — at the one boundary both backends share. A literal ``NULL``
# is *differently* broken on each: SQLite admits duplicate rows under the key and
# upserts insert rather than update, so the partition would accrete a row per
# heal with the Σ-invariant quietly counting all of them; Postgres rejects the
# row outright. The silent breakage is the default path's, which is why this is a
# sentinel rather than a ``NULL`` (ADR-0043 §2). The literal itself is `domain`'s
# because the validators that reserve it live above this layer.

# The position columns, in write order. The key leads; the money lines follow in
# the order ADR-0043 §3's DDL lists them.
POSITION_COLUMNS: tuple[str, ...] = (
    "strategy_id",
    "symbol",
    "signed_size",
    "entry_price",
    "realized_pnl",
    "fees",
    "funding",
    "isolated_collateral",
    "ts_ns",
)

POSITION_COLUMN_LIST = ", ".join(POSITION_COLUMNS)

POSITION_KEY_COLUMNS: tuple[str, ...] = ("strategy_id", "symbol")

# What an upsert may move: every column but the key it collided on.
POSITION_UPDATE_COLUMNS: tuple[str, ...] = tuple(
    column for column in POSITION_COLUMNS if column not in POSITION_KEY_COLUMNS
)


def position_values(position: Position, *, ts_ns: int) -> tuple[Any, ...]:
    """The position write tuple, in ``POSITION_COLUMNS`` order.

    ``entry_price`` is ``NULL`` on a flat row (ADR-0043 §3): ``NULL`` says "no
    position to have an entry for", where ``0`` would be indistinguishable from a
    real price. A full close resets entry (P1 #119), so ``is_flat`` is the whole
    of the condition — a row with no exposure has no entry worth keeping.

    ``isolated_collateral`` is nullable for the parallel reason — ``NULL`` is
    *cross-margined*, distinct from an isolated position wiped to ``0`` — but the
    write is unconditional here, because margin mode does not yet exist in the
    model to condition on (ADR-0040 §5 makes it config-authoritative; #190/#176
    land it). The column is in its final shape; **the slice that introduces a
    cross-margined position owns this write branch**, not just the field — the
    read half is already here, because a ``NULL`` the mapping cannot restore is a
    row the schema admits and the recovery mass-read would crash on.
    """
    return (
        UNATTRIBUTED if position.strategy_id is None else position.strategy_id,
        position.symbol,
        str(position.signed_size),
        None if position.is_flat else str(position.entry_price),
        str(position.realized_pnl),
        str(position.fees),
        str(position.funding),
        str(position.isolated_collateral),
        ts_ns,
    )


def restore_position(row: Sequence[Any]) -> Position:
    """One position row, in ``POSITION_COLUMNS`` order, back into a ``Position``.

    A ``NULL`` ``entry_price`` restores to ``0`` rather than to ``None``, because
    the aggregate has no ``None`` to hold: ADR-0041 §3 has a flat-with-history
    record read ``entry_price=0`` through the seam. The distinction the column
    keeps is a *durable* one — a reader of the table can tell "never had an
    entry" from a real price — and it costs nothing in memory, where ``is_flat``
    already answers the same question.

    ``isolated_collateral`` reads the same way, and **both** nullable columns are
    handled here even though only one of them has a writer today: the schema
    admits a ``NULL`` in either, and ``Decimal(None)`` raises ``TypeError`` —
    which is not a driver error, so it would escape ``all_positions()`` unwrapped,
    past the seam's ``InvariantViolation`` contract, on the recovery path. What
    the deferral to #190/#176 covers is the *write* branch — emitting ``NULL``
    for a genuinely cross-margined position — not this one.
    """
    return Position(
        strategy_id=None if row[0] == UNATTRIBUTED else row[0],
        symbol=row[1],
        signed_size=Decimal(row[2]),
        entry_price=_ZERO if row[3] is None else Decimal(row[3]),
        realized_pnl=Decimal(row[4]),
        fees=Decimal(row[5]),
        funding=Decimal(row[6]),
        isolated_collateral=_ZERO if row[7] is None else Decimal(row[7]),
    )


# The funding watermark row (ADR-0043 §5.2). Both timestamps are ``INTEGER``,
# not ``TEXT``: a boundary instant is a timestamp rather than money, so
# ADR-0029's ``Decimal``-as-``TEXT`` rule does not reach it.
FUNDING_MARK_COLUMNS: tuple[str, ...] = ("symbol", "last_funding_ts_ns", "ts_ns")

FUNDING_MARK_KEY_COLUMNS: tuple[str, ...] = ("symbol",)

FUNDING_MARK_UPDATE_COLUMNS: tuple[str, ...] = ("last_funding_ts_ns", "ts_ns")


def funding_mark_values(funding_mark: tuple[str, int], *, ts_ns: int) -> tuple[Any, ...]:
    """The watermark write tuple, in ``FUNDING_MARK_COLUMNS`` order."""
    symbol, boundary_ts_ns = funding_mark
    return (symbol, boundary_ts_ns, ts_ns)


def restore_account(row: Sequence[Any]) -> Account:
    """One account row, in ``ACCOUNT_COLUMNS`` order, back into an ``Account``."""
    return Account.restore(
        account_id=row[0],
        genesis_collateral=Decimal(row[1]),
        genesis_ts_ns=row[2],
        cash=Decimal(row[3]),
    )


_NO_LITERALS: Mapping[str, str] = MappingProxyType({})


def _upsert(
    *,
    table: str,
    columns: Sequence[str],
    key_columns: Sequence[str],
    update_columns: Sequence[str],
    placeholder: str,
    literals: Mapping[str, str] = _NO_LITERALS,
) -> str:
    """One ``INSERT … ON CONFLICT … DO UPDATE``, in the caller's dialect.

    ``columns`` are the parameterized columns in write-tuple order, so the
    statement and the tuple are rendered from one list and cannot fall out of
    step. ``literals`` are columns written as SQL literals instead — the
    ``account`` row's ``id = 1``, which is a constraint (ADR-0043 §3) rather than
    a value any caller supplies.

    ``update_columns`` is the load-bearing argument: it is what a collision may
    move. Every non-key column for a saga or position row; every column but the
    key *and* the write-once trio for the account row, which is what "written
    once, with the row; never updated" means in DDL.

    ``EXCLUDED`` is spelled once, uppercase — SQL keywords are case-insensitive,
    so both backends read the same text and the statement stays one string.
    """
    return (
        f"INSERT INTO {table} ({', '.join((*literals, *columns))}) "
        f"VALUES ({', '.join((*literals.values(), *([placeholder] * len(columns))))}) "
        f"ON CONFLICT ({', '.join(key_columns)}) DO UPDATE SET "
        + ", ".join(f"{column} = EXCLUDED.{column}" for column in update_columns)
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class Upserts:
    """Every durable write the store makes, rendered for one backend's dialect.

    One object rather than four constants per adapter, because the four
    statements express one decision — what a row is, and which of its columns a
    re-write may move — and that decision has no per-backend half. The adapter
    holds the dialect; this holds the semantics.
    """

    order: str
    account: str
    position: str
    funding_mark: str


def upserts_for(placeholder: str) -> Upserts:
    """The store's four upserts, parameterized by ``placeholder`` (``?`` / ``%s``).

    That marker is the whole of what used to differ between the two adapters'
    write SQL, which was otherwise transcribed twice from these same tuples. A
    new ledger table now adds a field here and is written identically by both
    backends, rather than by two hand-kept statements that agree right up until
    one of them is edited.
    """
    return Upserts(
        order=_upsert(
            table="orders",
            columns=RECORD_COLUMNS,
            key_columns=RECORD_KEY_COLUMNS,
            update_columns=RECORD_UPDATE_COLUMNS,
            placeholder=placeholder,
        ),
        account=_upsert(
            table="account",
            columns=ACCOUNT_COLUMNS,
            key_columns=ACCOUNT_KEY_COLUMNS,
            update_columns=ACCOUNT_UPDATE_COLUMNS,
            placeholder=placeholder,
            literals={"id": "1"},
        ),
        position=_upsert(
            table="positions",
            columns=POSITION_COLUMNS,
            key_columns=POSITION_KEY_COLUMNS,
            update_columns=POSITION_UPDATE_COLUMNS,
            placeholder=placeholder,
        ),
        funding_mark=_upsert(
            table="funding_marks",
            columns=FUNDING_MARK_COLUMNS,
            key_columns=FUNDING_MARK_KEY_COLUMNS,
            update_columns=FUNDING_MARK_UPDATE_COLUMNS,
            placeholder=placeholder,
        ),
    )
