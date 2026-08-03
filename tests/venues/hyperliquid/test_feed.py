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

import pytest
from hyperliquid_fakes import FakeWsConnection, asset_ctx_frame, trade, trades_frame
from structlog.typing import EventDict

from tickwright.adapters.bus import InMemoryBus
from tickwright.adapters.clock import ManualClock
from tickwright.domain import AggressorSide, MarketTick, MarkTick
from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid import HyperliquidConfig, HyperliquidFeed

_FIXTURES = Path(__file__).parent / "fixtures"


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


def test_prices_and_sizes_parse_to_decimal_never_float() -> None:
    frames = [trades_frame(trade("BTC", "0.1", 1, sz="0.2"))]
    seen, _ = _drive(frames, symbols=["BTC"], until_ticks=1)

    assert isinstance(seen[0].price, Decimal)
    assert isinstance(seen[0].size, Decimal)
    assert seen[0].price == Decimal("0.1")  # exact — a float round-trip would not be


def test_batched_trades_frame_yields_one_tick_per_trade_in_order() -> None:
    frames = [trades_frame(trade("BTC", "100", 1), trade("BTC", "101", 2))]
    seen, _ = _drive(frames, symbols=["BTC"], until_ticks=2)

    assert [t.trade_id for t in seen] == ["1", "2"]


def test_assigns_per_symbol_source_sequence() -> None:
    frames = [
        trades_frame(trade("BTC", "100", 1)),
        trades_frame(trade("ETH", "50", 2)),
        trades_frame(trade("BTC", "101", 3)),
    ]
    seen, _ = _drive(frames, symbols=["BTC", "ETH"], until_ticks=3)

    seqs = {(t.symbol, t.seq) for t in seen}
    assert seqs == {("BTC", 0), ("BTC", 1), ("ETH", 0)}


def test_live_ticks_dedup_on_the_venue_trade_id() -> None:
    frames = [trades_frame(trade("BTC", "100", 900000000000001))]
    seen, _ = _drive(frames, symbols=["BTC"], until_ticks=1)

    # Live-form weak key (ADR-0027): {symbol}:{tid}, not the replay form.
    assert seen[0].event_id == "BTC:900000000000001"


def test_slow_consumer_gets_only_the_latest_tick_per_symbol_with_one_lagged_per_drop() -> None:
    """ADR-0023: under backpressure the feed conflates — newest tick per symbol
    wins, every dropped tick emits one ``feed.lagged`` — while the WS keeps
    draining. Here the first publish blocks; three more ticks arrive meanwhile
    (BTC 101 → superseded by BTC 102 → one drop; ETH 50 kept)."""

    async def main() -> tuple[list[MarketTick], list[EventDict]]:
        bus = InMemoryBus()
        clock = ManualClock()
        seen: list[MarketTick] = []
        release = asyncio.Event()
        first_delivered = asyncio.Event()

        async def slow_consumer(tick: MarketTick) -> None:
            seen.append(tick)
            if len(seen) == 1:
                first_delivered.set()
                await release.wait()

        bus.subscribe(MarketTick, slow_consumer)
        connection = FakeWsConnection(
            [
                trades_frame(trade("BTC", "100", 1)),
                trades_frame(trade("BTC", "101", 2)),
                trades_frame(trade("BTC", "102", 3)),
                trades_frame(trade("ETH", "50", 4)),
            ]
        )

        async def connect(url: str) -> FakeWsConnection:
            return connection

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC", "ETH"]),
            bus=bus,
            clock=clock,
            connect=connect,
        )
        with capture_events() as logs:
            run = asyncio.create_task(feed.start())
            await asyncio.wait_for(first_delivered.wait(), timeout=2)
            # The publish is stuck in the slow consumer; the reader must still
            # drain the socket to the end before we let the consumer go.
            await asyncio.wait_for(connection.drained.wait(), timeout=2)
            release.set()

            async def rest_published() -> None:
                while len(seen) < 3:
                    await asyncio.sleep(0)

            await asyncio.wait_for(rest_published(), timeout=2)
            await feed.stop()
            await asyncio.wait_for(run, timeout=2)
        return seen, [log for log in logs if log["event"] == "feed.lagged"]

    seen, lagged = asyncio.run(main())

    # Latest per symbol only: BTC 101 was superseded while the consumer stalled.
    assert [(t.symbol, t.price) for t in seen] == [
        ("BTC", Decimal("100")),
        ("BTC", Decimal("102")),
        ("ETH", Decimal("50")),
    ]
    # Exactly one drop, named per ADR-0020/0023, identifying the dropped tick.
    assert len(lagged) == 1
    assert lagged[0]["symbol"] == "BTC"
    assert lagged[0]["dropped_trade_id"] == "2"


class RecordingClock(ManualClock):
    """A ``ManualClock`` that also records what it was asked to sleep — the
    backoff assertions read this, so no real time ever passes."""

    def __init__(self) -> None:
        super().__init__()
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await super().sleep(seconds)


def test_ws_drop_reconnects_with_backoff_resubscribes_and_resumes() -> None:
    """The venue hangs up after one tick, the next connect attempt is refused,
    the one after succeeds: the feed sleeps the doubling backoff on the injected
    clock (1s, then 2s — never a real sleep), resubscribes, and resumes."""

    async def main() -> tuple[list[MarketTick], FakeWsConnection, RecordingClock, int]:
        bus = InMemoryBus()
        clock = RecordingClock()
        seen: list[MarketTick] = []
        resumed = asyncio.Event()

        async def record(tick: MarketTick) -> None:
            seen.append(tick)
            if len(seen) >= 2:
                resumed.set()

        bus.subscribe(MarketTick, record)
        first = FakeWsConnection([trades_frame(trade("BTC", "100", 1))], drop_when_drained=True)
        second = FakeWsConnection([trades_frame(trade("BTC", "101", 2))])
        outcomes: list[FakeWsConnection | Exception] = [
            first,
            ConnectionRefusedError("venue hiccup"),
            second,
        ]
        connects = 0

        async def connect(url: str) -> FakeWsConnection:
            nonlocal connects
            connects += 1
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]), bus=bus, clock=clock, connect=connect
        )
        run = asyncio.create_task(feed.start())
        await asyncio.wait_for(resumed.wait(), timeout=2)
        await feed.stop()
        await asyncio.wait_for(run, timeout=2)
        return seen, second, clock, connects

    seen, second, clock, connects = asyncio.run(main())

    assert [t.price for t in seen] == [Decimal("100"), Decimal("101")]  # resumed
    assert connects == 3
    assert clock.sleeps == [1.0, 2.0]  # doubling backoff, virtual time only
    assert json.loads(second.sent[0]) == {  # resubscribed on the new socket
        "method": "subscribe",
        "subscription": {"type": "trades", "coin": "BTC"},
    }


def test_stop_does_not_trigger_a_reconnect() -> None:
    connection = FakeWsConnection([trades_frame(trade("BTC", "100", 1))])
    connect_count = 0

    async def main() -> None:
        nonlocal connect_count
        bus = InMemoryBus()
        got_one = asyncio.Event()

        async def record(tick: MarketTick) -> None:
            got_one.set()

        bus.subscribe(MarketTick, record)

        async def connect(url: str) -> FakeWsConnection:
            nonlocal connect_count
            connect_count += 1
            return connection

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]), bus=bus, clock=ManualClock(), connect=connect
        )
        run = asyncio.create_task(feed.start())
        await asyncio.wait_for(got_one.wait(), timeout=2)
        await feed.stop()
        await asyncio.wait_for(run, timeout=2)

    asyncio.run(main())
    assert connect_count == 1


def _drive_marks(
    frames: list[str], *, symbols: list[str], until_marks: int
) -> tuple[list[MarkTick], FakeWsConnection]:
    """``_drive``'s mark twin: run until ``until_marks`` marks arrive, then stop."""

    async def main() -> tuple[list[MarkTick], FakeWsConnection]:
        bus = InMemoryBus()
        seen: list[MarkTick] = []
        enough = asyncio.Event()

        async def record(mark: MarkTick) -> None:
            seen.append(mark)
            if len(seen) >= until_marks:
                enough.set()

        bus.subscribe(MarkTick, record)
        connection = FakeWsConnection(frames)

        async def connect(url: str) -> FakeWsConnection:
            return connection

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=symbols),
            bus=bus,
            clock=ManualClock(start_ns=4_000),
            connect=connect,
        )
        run = asyncio.create_task(feed.start())
        await asyncio.wait_for(enough.wait(), timeout=2)
        await feed.stop()
        await asyncio.wait_for(run, timeout=2)
        return seen, connection

    return asyncio.run(main())


def test_the_active_asset_ctx_channel_yields_one_mark_per_update() -> None:
    """The live mark's ingress (ADR-0039): ``ctx.markPx``, and nothing else in
    that context.

    ``oraclePx`` is funding's price and ``midPx`` is the book's — both ride the
    same frame, and either would value a position against a number the venue
    does not margin it with. The channel carries no timestamp of its own, so
    ``ts_event`` is receipt time off the injected clock (ADR-0005/0039).
    """
    frames = [asset_ctx_frame("BTC", "43251.0")]
    seen, _ = _drive_marks(frames, symbols=["BTC"], until_marks=1)

    mark = seen[0]
    assert mark.symbol == "BTC"
    assert mark.price == Decimal("43251.0")
    assert mark.ts_event == 4_000
    # The live form of the weak key: no ``seq``, because receipt time already
    # separates two marks on a channel that updates every few seconds.
    assert mark.event_id == "BTC:4000"


def test_the_mark_channel_is_subscribed_per_symbol_beside_the_trades_one() -> None:
    """Both channels, per coin, on one connection — and both **unauthenticated**,
    so the live feed still needs no key at all (ADR-0021)."""
    frames = [asset_ctx_frame("BTC", "100")]
    _, connection = _drive_marks(frames, symbols=["BTC", "ETH"], until_marks=1)

    assert [json.loads(m) for m in connection.sent] == [
        {"method": "subscribe", "subscription": {"type": "trades", "coin": "BTC"}},
        {"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": "BTC"}},
        {"method": "subscribe", "subscription": {"type": "trades", "coin": "ETH"}},
        {"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": "ETH"}},
    ]


def test_mark_prices_parse_to_decimal_never_float() -> None:
    frames = [asset_ctx_frame("BTC", "0.1")]
    seen, _ = _drive_marks(frames, symbols=["BTC"], until_marks=1)

    assert isinstance(seen[0].price, Decimal)
    assert seen[0].price == Decimal("0.1")  # exact — a float round-trip would not be


@pytest.mark.parametrize("figure", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_mark_is_dropped_rather_than_valuing_a_position(figure: str) -> None:
    """The same refusal the tick figures get, and it matters more here: a
    ``NaN`` mark would not merely mis-publish, it would propagate into every
    Tier-2 number the projection recomputes from it and surface as an
    ``InvalidOperation`` from some comparison far downstream."""
    frames = [asset_ctx_frame("BTC", figure), asset_ctx_frame("BTC", "100")]
    with capture_events() as logs:
        seen, _ = _drive_marks(frames, symbols=["BTC"], until_marks=1)

    assert [m.price for m in seen] == [Decimal("100")]
    assert [record["event"] for record in logs] == ["feed.frame_dropped"]


def test_non_trades_frames_are_ignored() -> None:
    frames = [
        json.dumps({"channel": "subscriptionResponse", "data": {"method": "subscribe"}}),
        json.dumps({"channel": "pong"}),
        trades_frame(trade("BTC", "100", 1)),
    ]
    seen, _ = _drive(frames, symbols=["BTC"], until_ticks=1)

    assert [t.trade_id for t in seen] == ["1"]


@pytest.mark.parametrize("figure", ["NaN", "Infinity", "-Infinity"])
def test_a_non_finite_tick_figure_is_dropped_not_ticked(figure: str) -> None:
    """``Decimal("NaN")``/``Decimal("Infinity")`` are *valid* constructions, so a
    non-finite ``px``/``sz`` is the one unreadable venue figure that raises
    nothing on the way in. It must be a dropped row like any other: a ``NaN``
    tick does not clear a downstream band check quietly — ``Decimal`` ordering
    *signals* on it — it detonates one layers away from the read that admitted
    it, and its equality and arithmetic are silently wrong all the way there."""

    async def main() -> tuple[list[MarketTick], list[EventDict]]:
        bus = InMemoryBus()
        clock = ManualClock()
        seen: list[MarketTick] = []
        enough = asyncio.Event()

        async def record(tick: MarketTick) -> None:
            seen.append(tick)
            enough.set()

        bus.subscribe(MarketTick, record)
        connection = FakeWsConnection(
            [
                trades_frame(trade("BTC", figure, 1)),  # non-finite price
                trades_frame(trade("BTC", "100", 2, sz=figure)),  # non-finite size
                trades_frame(trade("BTC", "100", 3)),  # the sentinel that must arrive
            ]
        )

        async def connect(url: str) -> FakeWsConnection:
            return connection

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]), bus=bus, clock=clock, connect=connect
        )
        with capture_events() as logs:
            run = asyncio.create_task(feed.start())
            await asyncio.wait_for(enough.wait(), timeout=2)
            await feed.stop()
            await asyncio.wait_for(run, timeout=2)
        return seen, [log for log in logs if log["event"] == "feed.frame_dropped"]

    seen, dropped = asyncio.run(main())

    # Only the finite trade reached the bus; neither non-finite row became a tick.
    assert [t.trade_id for t in seen] == ["3"]
    assert len(dropped) == 2


@pytest.mark.parametrize("figure", [100.5, 100, 1e30, 43250.123456789012345])
def test_a_re_typed_tick_figure_is_dropped_not_coerced(figure: object) -> None:
    """A ``px``/``sz`` the venue re-types as a JSON *number* is a dropped row.

    The venue reports both as decimal strings — first-party ``WsTrade`` is
    ``px: string, sz: string`` — so a number is the venue changing its contract,
    the same "we are not reading what we think we are" a missing field means. It
    cannot be coerced through either, and not because of our own parse —
    ``Decimal(str(x))`` would round-trip ``0.002`` exactly. ``json.loads`` is
    where it goes: a JSON number is a ``float`` before this reader sees it, so a
    reported ``43250.123456789012345`` arrives as ``43250.12345678901`` and a
    reported ``0.10`` is indistinguishable from ``0.1``. Neither is recoverable
    downstream, so the tick is no longer the exact figure ADR-0029 builds every
    price on — durable once a fill computed against it is written.

    The account grain has frozen on this since #217; the tick grain waved it
    through, which is the divergence this closes: one venue, one contract.
    """

    async def main() -> tuple[list[MarketTick], list[EventDict]]:
        bus = InMemoryBus()
        clock = ManualClock()
        seen: list[MarketTick] = []
        enough = asyncio.Event()

        async def record(tick: MarketTick) -> None:
            seen.append(tick)
            enough.set()

        bus.subscribe(MarketTick, record)
        connection = FakeWsConnection(
            [
                trades_frame(trade("BTC", figure, 1)),  # re-typed price
                trades_frame(trade("BTC", "100", 2, sz=figure)),  # re-typed size
                trades_frame(trade("BTC", "100", 3)),  # the sentinel that must arrive
            ]
        )

        async def connect(url: str) -> FakeWsConnection:
            return connection

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]), bus=bus, clock=clock, connect=connect
        )
        with capture_events() as logs:
            run = asyncio.create_task(feed.start())
            await asyncio.wait_for(enough.wait(), timeout=2)
            await feed.stop()
            await asyncio.wait_for(run, timeout=2)
        return seen, [log for log in logs if log["event"] == "feed.frame_dropped"]

    seen, dropped = asyncio.run(main())

    assert [t.trade_id for t in seen] == ["3"]
    assert len(dropped) == 2


def test_malformed_frames_are_skipped_and_named_while_good_frames_keep_flowing() -> None:
    """A corrupt frame or trade row must never fault the feed (ADR-0023): each
    emits one ``feed.frame_dropped`` and is skipped, and good rows — even in the
    same batch as a bad one — still tick through."""

    async def main() -> tuple[list[MarketTick], list[EventDict]]:
        bus = InMemoryBus()
        clock = ManualClock()
        seen: list[MarketTick] = []
        enough = asyncio.Event()

        async def record(tick: MarketTick) -> None:
            seen.append(tick)
            if len(seen) >= 2:
                enough.set()

        bus.subscribe(MarketTick, record)
        connection = FakeWsConnection(
            [
                "}not json{",  # frame-level garbage → one drop
                json.dumps({"channel": "trades", "data": "oops"}),  # trades frame, data not a list
                trades_frame({"coin": "BTC", "side": "B", "px": "nope"}),  # unparseable row
                trades_frame(trade("BTC", "100", 1), {"coin": "BTC"}),  # one good, one bad row
                trades_frame(trade("BTC", "101", 2), trade("BTC", "NaN", 9)),  # good + non-finite
            ]
        )

        async def connect(url: str) -> FakeWsConnection:
            return connection

        feed = HyperliquidFeed(
            config=HyperliquidConfig(symbols=["BTC"]), bus=bus, clock=clock, connect=connect
        )
        with capture_events() as logs:
            run = asyncio.create_task(feed.start())
            await asyncio.wait_for(enough.wait(), timeout=2)
            await feed.stop()
            await asyncio.wait_for(run, timeout=2)
        return seen, [log for log in logs if log["event"] == "feed.frame_dropped"]

    seen, dropped = asyncio.run(main())

    # Both good trades ticked through despite the garbage around and beside them.
    assert [t.trade_id for t in seen] == ["1", "2"]
    # One drop each: the non-JSON frame, the non-list `data`, the bad-only row,
    # the bad row in the mixed batch, and the non-finite row beside trade 2 —
    # a figure that is not a number drops at row grain like any other, so the
    # good trade batched with it is unaffected.
    assert len(dropped) == 5
