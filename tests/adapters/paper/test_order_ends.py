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


def _run(signal: PlaceSignal, *, partial_fill_fraction: str) -> Order | None:
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
        await manager.on_signal(signal)
        return store.get_order(derive_cloid(signal.signal_id))

    return asyncio.run(main())


def test_an_ioc_order_filled_in_part_ends_cancelled_with_its_fill_kept() -> None:
    ioc_buy = PlaceSignal(
        ts_event=NS,
        ts_init=NS,
        strategy_id="paper",
        symbol="BTC",
        seq=1,
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.IOC,
        price=Decimal("42000"),
    )

    order = _run(ioc_buy, partial_fill_fraction="0.4")

    assert_order_ends(
        order, case=OrderEnd.IOC_PARTIAL_FILL, filled=Decimal("0.4"), name="PaperExchange"
    )
