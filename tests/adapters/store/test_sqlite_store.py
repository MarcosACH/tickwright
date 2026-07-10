"""SQLite-specific ``Store`` behavior (ADR-0019).

The cross-backend contract — round-trips, history, dedup, durability across a
reopen, upserts, kill-switch — lives in ``test_store_contract.py`` and runs
against both adapters. What remains here is the one thing only ``SQLiteStore``
has: the ``:memory:`` default, the zero-setup in-process store the hermetic
paper + in-memory-bus path recovers from with nothing installed.
"""

from decimal import Decimal

from tickwright.adapters.store import SQLiteStore
from tickwright.domain import Order, OrderState, OrderSubmitted, OrderType, Side


def _order() -> Order:
    return Order(
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("2"),
        order_type=OrderType.MARKET,
    )


def _submitted() -> OrderSubmitted:
    return OrderSubmitted(
        ts_event=1,
        ts_init=1,
        cloid="0xabc",
        strategy_id="trivial",
        signal_id="trivial:BTC:1",
        symbol="BTC",
        venue_oid="oid-1",
    )


def test_in_memory_default_round_trips_a_checkpoint_within_the_session() -> None:
    store = SQLiteStore()  # ":memory:" — the zero-setup default
    order = _order()
    order.apply(_submitted())
    store.checkpoint(order, ts_ns=1_000)

    loaded = store.get_order("0xabc")
    assert loaded is not None
    assert loaded.state is OrderState.SUBMITTED
    store.close()
