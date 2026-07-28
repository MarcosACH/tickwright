"""Shared record (de)serialization for the ``Store`` adapters (ADR-0019).

Both ``SQLiteStore`` and ``PostgresStore`` persist the same rows: the same
columns, the same JSON encoding of the applied-event dedup set and the
transition history, the same ``Decimal``-as-``TEXT`` money mapping. Only the SQL
*dialect* differs — placeholder syntax and the upsert clause. This module owns
the field mapping so the two backends cannot drift on what a row *is*; each
backend keeps only its own SQL.

Money is written ``str(value)`` and read ``Decimal(text)``, exact in
*representation* rather than merely in numeric value: trailing zeros, ``-0`` and
exponent forms all survive, because ``str(Decimal)`` preserves coefficient and
exponent (ADR-0043 §7).
"""

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from tickwright.domain import Account, Order, OrderState, OrderType, Position, Side

# The saga columns, in write order. ``history`` (the ADR-0008 checkpoint trail)
# is last: it is an adapter-only audit surface, not part of what recovery reads.
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
    """The position write tuple, in ``POSITION_COLUMNS`` order."""
    return (
        position.strategy_id,
        position.symbol,
        str(position.signed_size),
        str(position.entry_price),
        str(position.realized_pnl),
        str(position.fees),
        str(position.funding),
        str(position.isolated_collateral),
        ts_ns,
    )


def restore_position(row: Sequence[Any]) -> Position:
    """One position row, in ``POSITION_COLUMNS`` order, back into a ``Position``."""
    return Position(
        strategy_id=row[0],
        symbol=row[1],
        signed_size=Decimal(row[2]),
        entry_price=Decimal(row[3]),
        realized_pnl=Decimal(row[4]),
        fees=Decimal(row[5]),
        funding=Decimal(row[6]),
        isolated_collateral=Decimal(row[7]),
    )


def restore_account(row: Sequence[Any]) -> Account:
    """One account row, in ``ACCOUNT_COLUMNS`` order, back into an ``Account``."""
    return Account.restore(
        account_id=row[0],
        genesis_collateral=Decimal(row[1]),
        genesis_ts_ns=row[2],
        cash=Decimal(row[3]),
    )
