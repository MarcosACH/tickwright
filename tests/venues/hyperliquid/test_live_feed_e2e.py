"""The live-feed-shaped E2E (issue #22): mocked Hyperliquid frames through the
whole pipeline — ``HyperliquidFeed`` → ``SingleShotMarketStrategy`` →
``ExecutionManager`` → ``PaperExchange`` fill → strategy sees ``OrderFilled``.

The WS connection is the only fake; every engine component is real, the run is
on ``ManualClock``, and nothing touches the network — the live path's data
shape (venue trade ids, ms-sourced timestamps) crosses every layer.
"""

import asyncio
from decimal import Decimal

from hyperliquid_fakes import FakeWsConnection, trade, trades_frame
from ledgers import GENESIS, ledger

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.adapters.paper import ImmediateFillModel, PaperExchange
from tickwright.adapters.store import SQLiteStore
from tickwright.domain import (
    ExecutionReport,
    MarketTick,
    OrderEvent,
    OrderFilled,
    Side,
    Signal,
)
from tickwright.engine.cache import Cache
from tickwright.engine.execution import ExecutionManager
from tickwright.strategies import SingleShotMarketStrategy
from tickwright.venues.hyperliquid import HyperliquidConfig, HyperliquidFeed


def test_mocked_frames_reach_a_paper_fill_through_the_whole_pipeline() -> None:
    async def main() -> SingleShotMarketStrategy:
        bus = InMemoryBus()
        clock = ManualClock()
        exchange = PaperExchange(
            bus=bus, clock=clock, fill_model=ImmediateFillModel(), genesis_collateral=GENESIS
        )
        store = SQLiteStore(":memory:")
        projection = ledger(store)
        manager = ExecutionManager(
            bus=bus,
            clock=clock,
            store=store,
            exchange=exchange,
            cache=Cache(store=store),
            portfolio=projection,
        )
        strategy = SingleShotMarketStrategy(
            strategy_id="live",
            bus=bus,
            clock=clock,
            portfolio=projection.for_strategy("live"),
            side=Side.BUY,
            quantity=Decimal("0.5"),
        )
        connection = FakeWsConnection([trades_frame(trade("BTC", "43250.5", 1, sz="3"))])

        async def connect(url: str) -> FakeWsConnection:
            return connection

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]), bus=bus, clock=clock, connect=connect
        )

        bus.subscribe(MarketTick, strategy.on_tick)
        bus.subscribe(Signal, manager.on_signal)
        bus.subscribe(ExecutionReport, manager.on_execution_report)
        bus.subscribe(OrderEvent, strategy.on_order_event)

        filled = asyncio.Event()

        async def see_fill(event: OrderEvent) -> None:
            if isinstance(event, OrderFilled):
                filled.set()

        bus.subscribe(OrderEvent, see_fill)

        run = asyncio.create_task(feed.start())
        await asyncio.wait_for(filled.wait(), timeout=2)
        await feed.stop()
        await asyncio.wait_for(run, timeout=2)
        return strategy

    strategy = asyncio.run(main())

    assert len(strategy.fills) == 1
    fill = strategy.fills[0]
    assert isinstance(fill, OrderFilled)
    # Filled at the mocked venue trade's price: live-feed-shaped data drove the
    # strategy, the saga, and the paper venue end to end.
    assert fill.price == Decimal("43250.5")
    assert fill.quantity == Decimal("0.5")
    assert fill.signal_id == "live:BTC:1"
