"""Paper keeps its resting orders across a restart on the same store (#484).

Paper holds its book in memory only. A new process builds a new venue, so the
venue rebuilds its book from the order rows the store kept. Each case builds
paper the way ``app/build.py`` does, through ``paper_store_reads``.
"""

import asyncio
from decimal import Decimal

from ledgers import GENESIS

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.app.build import paper_store_reads
from tickwright.domain import (
    Order,
    OrderState,
    OrderType,
    Side,
    TimeInForce,
    VenueOpenOrder,
)


def _paper(store: SQLiteStore) -> PaperExchange:
    reads = paper_store_reads(store)
    return PaperExchange(
        bus=InMemoryBus(),
        clock=ManualClock(start_ns=1_000),
        fill_model=ImmediateFillModel(),
        genesis_collateral=GENESIS,
        account_net=reads.account_net,
        applied_fills=reads.applied_fills,
        resting_orders=reads.resting_orders,
    )


def _resting(cloid: str = "0xa", *, cum_qty: str = "0") -> Order:
    """A limit an earlier run left resting, as its order row holds it."""
    return Order.restore(
        cloid=cloid,
        strategy_id="trivial",
        signal_id=f"trivial:BTC:{cloid}",
        symbol="BTC",
        side=Side.BUY,
        quantity=Decimal("2"),
        order_type=OrderType.LIMIT,
        state=OrderState.LIVE,
        cum_qty=Decimal(cum_qty),
        venue_oid=None,
        reason=None,
        cancel_requested=False,
        cancel_requested_ts=None,
        cancel_signal_id=None,
        applied_event_ids=(),
        price=Decimal("41000"),
        time_in_force=TimeInForce.GTC,
    )


def test_a_restarted_paper_venue_lists_the_orders_an_earlier_run_left_resting() -> None:
    store = SQLiteStore(":memory:")
    store.checkpoint(_resting("0xa"), ts_ns=1_000)
    exchange = _paper(store)

    async def scenario() -> object:
        await exchange.start()
        return await exchange.fetch_open_orders()

    assert asyncio.run(scenario()) == [VenueOpenOrder(symbol="BTC", cloid="0xa")]
