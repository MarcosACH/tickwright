"""Shared saga-record (de)serialization for the ``Store`` adapters (ADR-0019).

Both ``SQLiteStore`` and ``PostgresStore`` persist the same saga record: the
same columns, the same JSON encoding of the applied-event dedup set and the
transition history. Only the SQL *dialect* differs — placeholder syntax and the
upsert clause. This module owns the field mapping so the two backends cannot
drift on what a saga row *is*; each backend keeps only its own SQL.
"""

import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from tickwright.domain import Order, OrderState, OrderType, Side

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
