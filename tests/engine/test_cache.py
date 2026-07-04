"""``Cache`` — the in-memory read-model of current order state (ADR-0009).

A write-through projection of the ``Store``, never the source of truth: rebuilt
from the store on startup (recovery step 2), read by direct method call
(pull-then-subscribe), written only through the ``ExecutionManager``'s
checkpoint path. These tests pin the projection contract: what the store
persisted is exactly what the rebuilt cache serves.
"""

from decimal import Decimal

from tickwright.adapters.store import SQLiteStore
from tickwright.domain import Order, OrderType, Side
from tickwright.engine.cache import Cache


def _order(cloid: str, *, strategy_id: str = "trivial", symbol: str = "BTC") -> Order:
    return Order(
        cloid=cloid,
        strategy_id=strategy_id,
        signal_id=f"{strategy_id}:{symbol}:{cloid}",
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal("2"),
        order_type=OrderType.MARKET,
    )


def test_rebuilt_cache_serves_the_stores_checkpointed_saga() -> None:
    store = SQLiteStore(":memory:")
    store.checkpoint(_order("0xabc"), ts_ns=1)
    store.checkpoint(_order("0xdef"), ts_ns=2)

    cache = Cache(store=store)
    cache.rebuild()

    assert cache.get_order("0xabc") == store.get_order("0xabc")
    assert cache.get_order("0xdef") == store.get_order("0xdef")
    assert cache.get_order("0xmissing") is None
