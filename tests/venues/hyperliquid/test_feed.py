"""``HyperliquidFeed`` — recorded-frame tests, no network (ADR-0021/0023/0027).

The WS connection is the process boundary: ``FakeWsConnection`` replays
recorded ``trades``-channel frames (captured shape, see ``fixtures/``) and
records what the feed sends, so the default suite drives the live-feed code
path hermetically.
"""

import asyncio
import json
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


def _trades_frame(*trades: dict) -> str:
    return json.dumps({"channel": "trades", "data": list(trades)})


def _trade(
    coin: str, px: str, tid: int, *, side: str = "B", sz: str = "1", time: int = 1_700_000_000_000
) -> dict:
    return {"coin": coin, "side": side, "px": px, "sz": sz, "time": time, "tid": tid}


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


def test_subscribes_the_trades_channel_per_configured_symbol() -> None:
    frames = [_trades_frame(_trade("BTC", "100", 1))]
    _, connection = _drive(frames, symbols=["BTC", "ETH"], until_ticks=1)

    assert [json.loads(m) for m in connection.sent] == [
        {"method": "subscribe", "subscription": {"type": "trades", "coin": "BTC"}},
        {"method": "subscribe", "subscription": {"type": "trades", "coin": "ETH"}},
    ]


def test_prices_and_sizes_parse_to_decimal_never_float() -> None:
    frames = [_trades_frame(_trade("BTC", "0.1", 1, sz="0.2"))]
    seen, _ = _drive(frames, symbols=["BTC"], until_ticks=1)

    assert isinstance(seen[0].price, Decimal)
    assert isinstance(seen[0].size, Decimal)
    assert seen[0].price == Decimal("0.1")  # exact — a float round-trip would not be


def test_batched_trades_frame_yields_one_tick_per_trade_in_order() -> None:
    frames = [_trades_frame(_trade("BTC", "100", 1), _trade("BTC", "101", 2))]
    seen, _ = _drive(frames, symbols=["BTC"], until_ticks=2)

    assert [t.trade_id for t in seen] == ["1", "2"]


def test_assigns_per_symbol_source_sequence() -> None:
    frames = [
        _trades_frame(_trade("BTC", "100", 1)),
        _trades_frame(_trade("ETH", "50", 2)),
        _trades_frame(_trade("BTC", "101", 3)),
    ]
    seen, _ = _drive(frames, symbols=["BTC", "ETH"], until_ticks=3)

    seqs = {(t.symbol, t.seq) for t in seen}
    assert seqs == {("BTC", 0), ("BTC", 1), ("ETH", 0)}


def test_live_ticks_dedup_on_the_venue_trade_id() -> None:
    frames = [_trades_frame(_trade("BTC", "100", 900000000000001))]
    seen, _ = _drive(frames, symbols=["BTC"], until_ticks=1)

    # Live-form weak key (ADR-0027): {symbol}:{tid}, not the replay form.
    assert seen[0].event_id == "BTC:900000000000001"


def test_non_trades_frames_are_ignored() -> None:
    frames = [
        json.dumps({"channel": "subscriptionResponse", "data": {"method": "subscribe"}}),
        json.dumps({"channel": "pong"}),
        _trades_frame(_trade("BTC", "100", 1)),
    ]
    seen, _ = _drive(frames, symbols=["BTC"], until_ticks=1)

    assert [t.trade_id for t in seen] == ["1"]
