"""Prints Hyperliquid's live trades + mark prices as they arrive.

Wires the engine's real HyperliquidFeed straight to a print subscriber, with
no strategy, exchange, or store in the loop — just the market-data half, to
watch the feed run continuously (the bundled examples/ticks.jsonl is only 3
rows). No API key needed: both channels are public. Run: uv run python
prototypes/live_demo/watch_ticks.py — Ctrl-C to stop.
"""

import asyncio

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import LiveClock
from tickwright.domain import MarketTick, MarkTick
from tickwright.venues.hyperliquid import HyperliquidConfig
from tickwright.venues.hyperliquid.feed import HyperliquidFeed

SYMBOLS = ["BTC"]


async def on_trade(tick: MarketTick) -> None:
    print(f"TRADE {tick.symbol}  {tick.aggressor_side:<4}  px={tick.price}  sz={tick.size}")


async def on_mark(tick: MarkTick) -> None:
    print(f"MARK  {tick.symbol}  px={tick.price}")


async def main() -> None:
    bus = InMemoryBus()
    bus.subscribe(MarketTick, on_trade)
    bus.subscribe(MarkTick, on_mark)
    feed = HyperliquidFeed(config=HyperliquidConfig(symbols=SYMBOLS), bus=bus, clock=LiveClock())
    await feed.start()
    try:
        await feed.run()
    finally:
        await feed.stop()


if __name__ == "__main__":
    asyncio.run(main())
