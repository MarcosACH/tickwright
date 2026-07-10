"""``HyperliquidFeed`` — recorded-frame tests, no network (ADR-0021/0023/0027).

The WS connection is the process boundary: ``FakeWsConnection`` replays
recorded ``trades``-channel frames (captured shape, see ``fixtures/``) and
records what the feed sends, so the default suite drives the live-feed code
path hermetically.
"""

import asyncio
from decimal import Decimal
from pathlib import Path

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.domain import AggressorSide, MarketTick
from tickwright.venues.hyperliquid import HyperliquidConfig, HyperliquidFeed

_FIXTURES = Path(__file__).parent / "fixtures"


class FakeWsConnection:
    """A recorded-frame WS connection: replays frames, records sends, then idles
    (a live socket delivers nothing between trades) until closed."""

    def __init__(self, frames: list[str]) -> None:
        self.sent: list[str] = []
        self._frames = list(frames)
        self._closed = asyncio.Event()

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self) -> None:
        self._closed.set()

    def __aiter__(self) -> "FakeWsConnection":
        return self

    async def __anext__(self) -> str:
        if self._frames:
            return self._frames.pop(0)
        await self._closed.wait()
        raise StopAsyncIteration


def _fixture_frames() -> list[str]:
    text = (_FIXTURES / "trades.jsonl").read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def _drive(
    frames: list[str], *, symbols: list[str], until_ticks: int
) -> tuple[list[MarketTick], FakeWsConnection]:
    """Run the feed over ``frames`` until ``until_ticks`` ticks arrive, then stop."""

    async def main() -> tuple[list[MarketTick], FakeWsConnection]:
        bus = InMemoryBus()
        clock = ManualClock()
        seen: list[MarketTick] = []
        enough = asyncio.Event()

        async def record(tick: MarketTick) -> None:
            seen.append(tick)
            if len(seen) >= until_ticks:
                enough.set()

        bus.subscribe(MarketTick, record)
        connection = FakeWsConnection(frames)

        async def connect(url: str) -> FakeWsConnection:
            return connection

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=symbols),
            bus=bus,
            clock=clock,
            connect=connect,
        )
        run = asyncio.create_task(feed.start())
        await asyncio.wait_for(enough.wait(), timeout=2)
        await feed.stop()
        await asyncio.wait_for(run, timeout=2)
        return seen, connection

    return asyncio.run(main())


def test_recorded_trades_frames_parse_into_market_ticks() -> None:
    seen, _ = _drive(_fixture_frames(), symbols=["BTC"], until_ticks=2)

    buy, sell = seen
    assert buy.symbol == "BTC"
    assert buy.price == Decimal("43250.5")
    assert buy.size == Decimal("0.25")
    assert buy.aggressor_side is AggressorSide.BUY
    assert buy.trade_id == "900000000000001"
    assert buy.ts_event == 1_700_000_000_123 * 1_000_000  # venue ms → engine ns

    assert sell.aggressor_side is AggressorSide.SELL
    assert sell.price == Decimal("43249.0")
