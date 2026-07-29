"""``Cache`` — the in-memory read-model of current order state (ADR-0009).

A write-through projection of the ``Store``, never the source of truth: rebuilt
from the store on startup (recovery step 2), read by direct method call
(pull-then-subscribe), written only through the ``ExecutionManager``'s
checkpoint path. These tests pin the projection contract: what the store
persisted is exactly what the rebuilt cache serves.
"""

from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

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


def test_project_serves_the_order_without_writing_it() -> None:
    """The in-memory half on its own, for the one path whose durability is
    already paid for: a fill's order row rides the ledger's single transaction
    (ADR-0043 §4), so the caller needs the projection and nothing more.

    Writing here as well would be the split the atomicity argument exists to
    close — a second, separate transaction for the same order row.
    """
    store = SQLiteStore(":memory:")
    cache = Cache(store=store)
    order = _order("0xabc")

    cache.project(order, ts_ns=7)

    assert cache.get_order("0xabc") is order
    assert cache.last_event_ts("0xabc") == 7
    assert store.get_order("0xabc") is None


def test_last_event_ts_tracks_this_sessions_writes_but_a_rebuild_forgets_them() -> None:
    store = SQLiteStore(":memory:")
    store.checkpoint(_order("0xold"), ts_ns=500)
    cache = Cache(store=store)
    cache.rebuild()

    # A saga recovered from the store carries no session recency: it is not
    # "just-acked this session", so the ghost cycle reads no protection for it
    # (the startup barrier already re-proved it, ADR-0009/0011).
    assert cache.last_event_ts("0xold") is None

    # A write-through checkpoint stamps the recency the ghost cycle reads.
    cache.checkpoint(_order("0xnew"), ts_ns=1_700)
    assert cache.last_event_ts("0xnew") == 1_700
    # The latest write wins.
    cache.checkpoint(_order("0xnew"), ts_ns=2_400)
    assert cache.last_event_ts("0xnew") == 2_400

    # A rebuild discards session recency wholesale — a fresh process starts blank.
    cache.rebuild()
    assert cache.last_event_ts("0xnew") is None
    assert cache.last_event_ts("0xmissing") is None


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


@st.composite
def _saga_population(draw: st.DrawFn) -> list[Order]:
    """Arbitrary checkpointed sagas: every state, marker, and dedup-set shape."""
    count = draw(st.integers(min_value=0, max_value=8))
    orders = []
    for index in range(count):
        cancel_requested = draw(st.booleans())
        orders.append(
            Order.restore(
                cloid=f"0x{index:03d}",
                strategy_id=draw(st.sampled_from(["alpha", "beta"])),
                signal_id=f"sig-{index}",
                symbol=draw(st.sampled_from(["BTC", "ETH"])),
                side=draw(st.sampled_from(list(Side))),
                quantity=Decimal(draw(st.integers(min_value=1, max_value=1000))) / 100,
                order_type=draw(st.sampled_from(list(OrderType))),
                state=draw(st.sampled_from(list(OrderState))),
                cum_qty=Decimal(draw(st.integers(min_value=0, max_value=1000))) / 100,
                venue_oid=draw(st.none() | st.text(min_size=1, max_size=8)),
                reason=draw(st.none() | st.text(min_size=1, max_size=8)),
                cancel_requested=cancel_requested,
                cancel_requested_ts=(
                    # Any epoch-ns a Clock can mint (SQLite INTEGER is i64).
                    draw(st.integers(min_value=0, max_value=2**63 - 1))
                    if cancel_requested
                    else None
                ),
                cancel_signal_id=f"cxl-{index}" if cancel_requested else None,
                applied_event_ids=draw(st.lists(st.text(min_size=1, max_size=12), unique=True)),
            )
        )
    return orders


@given(orders=_saga_population())
def test_rebuilt_cache_equals_the_stores_projection_for_any_population(
    orders: list[Order],
) -> None:
    store = SQLiteStore(":memory:")
    for ts_ns, order in enumerate(orders):
        store.checkpoint(order, ts_ns=ts_ns)

    cache = Cache(store=store)
    cache.rebuild()

    # The projection is the store, field for field — dedup sets and cancel
    # markers included: recovery must lose nothing the checkpoint recorded.
    for order in orders:
        assert cache.get_order(order.cloid) == store.get_order(order.cloid) == order
    assert {o.cloid for o in cache.open_orders()} == {o.cloid for o in orders if not o.is_terminal}
