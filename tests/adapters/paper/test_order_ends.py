"""How paper ends an order, read off the saga (#632).

The real ``ExecutionManager`` against the real ``PaperExchange``. The saga is
what the strategy waits on, so the gate reads it and not the venue's reports.
"""

import asyncio
import random
from decimal import Decimal

from exchange_contract import OrderEnd, assert_order_ends
from ledgers import GENESIS, checkpointer

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import PaperExchange, StochasticFillModel, StochasticParams
from tickwright.adapters.store import SQLiteStore
from tickwright.app.build import paper_store_reads
from tickwright.domain import (
    AggressorSide,
    ExecutionReport,
    MarketTick,
    Order,
    OrderType,
    PlaceSignal,
    Side,
    TimeInForce,
    derive_cloid,
)
from tickwright.engine.execution import ExecutionManager

NS = 1_000


def _tick(price: str) -> MarketTick:
    return MarketTick(
        ts_event=NS,
        ts_init=NS,
        symbol="BTC",
        price=Decimal(price),
        size=Decimal("5"),
        aggressor_side=AggressorSide.BUY,
        trade_id="t1",
        seq=0,
    )


def _signal(
    seq: int,
    side: Side,
    quantity: str,
    *,
    limit_price: str | None = None,
    reduce_only: bool = False,
) -> PlaceSignal:
    """An IOC order. A market order unless it has a ``limit_price``."""
    return PlaceSignal(
        ts_event=NS,
        ts_init=NS,
        strategy_id="paper",
        symbol="BTC",
        seq=seq,
        side=side,
        quantity=Decimal(quantity),
        order_type=OrderType.MARKET if limit_price is None else OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        price=None if limit_price is None else Decimal(limit_price),
        reduce_only=reduce_only,
    )


def _run(*signals: PlaceSignal, partial_fill_fraction: str = "1") -> Order | None:
    """Send each signal in turn, and return the last one's saga."""

    async def main() -> Order | None:
        bus = InMemoryBus()
        clock = ManualClock(start_ns=NS)
        store = SQLiteStore(":memory:")
        reads = paper_store_reads(store)
        exchange = PaperExchange(
            bus=bus,
            clock=clock,
            fill_model=StochasticFillModel(
                rng=random.Random(0),
                clock=clock,
                params=StochasticParams(
                    prob_fill_on_limit=1.0,
                    partial_fill_fraction=Decimal(partial_fill_fraction),
                ),
            ),
            genesis_collateral=GENESIS,
            account_net=reads.account_net,
            applied_fills=reads.applied_fills,
            resting_orders=reads.resting_orders,
        )
        manager = ExecutionManager(
            bus=bus, exchange=exchange, checkpointer=checkpointer(store, clock=clock)
        )
        bus.subscribe(ExecutionReport, manager.on_execution_report)
        await bus.publish(_tick("41000"))
        for signal in signals:
            await manager.on_signal(signal)
        return store.get_order(derive_cloid(signals[-1].signal_id))

    return asyncio.run(main())


def test_an_ioc_order_filled_in_part_ends_cancelled_with_its_fill_kept() -> None:
    ioc_buy = _signal(1, Side.BUY, "1", limit_price="42000")

    order = _run(ioc_buy, partial_fill_fraction="0.4")

    assert_order_ends(
        order, case=OrderEnd.IOC_PARTIAL_FILL, filled=Decimal("0.4"), name="PaperExchange"
    )


def test_an_ioc_order_that_matches_nothing_ends_cancelled() -> None:
    # The last trade is 41000, so a buy limited to 40000 crosses nothing.
    ioc_buy = _signal(1, Side.BUY, "1", limit_price="40000")

    order = _run(ioc_buy)

    assert_order_ends(order, case=OrderEnd.IOC_NO_MATCH, filled=Decimal("0"), name="PaperExchange")


def test_a_reduce_only_order_cut_to_the_position_ends_cancelled_with_the_net_filled() -> None:
    open_long = _signal(1, Side.BUY, "0.3")
    close_more = _signal(2, Side.SELL, "0.5", reduce_only=True)

    order = _run(open_long, close_more)

    assert_order_ends(
        order, case=OrderEnd.REDUCE_ONLY_CUT, filled=Decimal("0.3"), name="PaperExchange"
    )
