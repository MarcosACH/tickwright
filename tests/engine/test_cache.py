"""``Cache`` — the in-memory read-model of current order state (ADR-0009).

A write-through projection of the ``Store``, never the source of truth: rebuilt
from the store on startup (recovery step 2), read by direct method call
(pull-then-subscribe), written only through the ``ExecutionManager``'s
checkpoint path. These tests pin the projection contract: what the store
persisted is exactly what the rebuilt cache serves.
"""

from decimal import Decimal

from tickwright.adapters.store import SQLiteStore
from tickwright.domain import Order, OrderState, OrderType, Side
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


def test_checkpoint_writes_through_to_the_store_and_the_projection() -> None:
    store = SQLiteStore(":memory:")
    cache = Cache(store=store)
    order = _order("0xabc")

    cache.checkpoint(order, ts_ns=1)

    # Durable first: the store carries the record, so a crash right after the
    # checkpoint recovers it — and the projection serves it with no rebuild.
    assert store.get_order("0xabc") == order
    assert cache.get_order("0xabc") is order


def test_open_orders_are_the_non_terminal_sagas_filtered_by_strategy_and_symbol() -> None:
    store = SQLiteStore(":memory:")
    cache = Cache(store=store)

    live = _order("0xlive")
    live.state = OrderState.LIVE
    other_strategy = _order("0xother", strategy_id="other")
    other_strategy.state = OrderState.LIVE
    other_symbol = _order("0xeth", symbol="ETH")
    filled = _order("0xdone")
    filled.state = OrderState.FILLED
    for order in (live, other_strategy, other_symbol, filled):
        cache.checkpoint(order, ts_ns=1)

    # Unfiltered, this is the reconciler's view: every saga still awaiting a
    # venue resolution — a PENDING intent is open exposure too (ADR-0008).
    assert cache.open_orders() == [live, other_strategy, other_symbol]
    assert cache.open_orders(strategy_id="trivial") == [live, other_symbol]
    assert cache.open_orders(strategy_id="trivial", symbol="BTC") == [live]
