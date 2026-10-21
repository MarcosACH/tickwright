"""``ScopedPortfolio`` read over the engine's real ``Cache`` (ADR-0041 §3).

Most open-order behavior is driven through the whole engine in
``test_open_orders_e2e.py``. A ``PENDING`` order cannot be reached that way. In
one life the saga leaves ``PENDING`` with no await in between, and after a crash
the startup barrier settles it before a strategy reads. So this suite checkpoints
the order the way the ``ExecutionManager`` does and reads it through the facade.
"""

from decimal import Decimal

from ledgers import checkpointer

from tickwright.adapters.clock import ManualClock
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import OpenOrderView, Order, OrderState, OrderType, Side, TimeInForce
from tickwright.engine.scoped_portfolio import ScopedPortfolio


def test_a_pending_intent_reads_as_open() -> None:
    checkpoints = checkpointer(SQLiteStore(":memory:"), clock=ManualClock())
    checkpoints.checkpoint(
        Order(
            cloid="0xpending",
            strategy_id="scripted",
            signal_id="scripted:BTC:1",
            symbol="BTC",
            side=Side.BUY,
            quantity=Decimal("0.1"),
            order_type=OrderType.LIMIT,
            price=Decimal("30000"),
            time_in_force=TimeInForce.GTC,
        )
    )
    portfolio = ScopedPortfolio(
        projection=checkpoints.portfolio, cache=checkpoints.cache, strategy_id="scripted"
    )

    assert portfolio.open_orders() == (
        OpenOrderView(
            signal_id="scripted:BTC:1",
            symbol="BTC",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            quantity=Decimal("0.1"),
            filled_quantity=Decimal("0"),
            price=Decimal("30000"),
            post_only=False,
            reduce_only=False,
            state=OrderState.PENDING,
            cancel_requested=False,
        ),
    )
