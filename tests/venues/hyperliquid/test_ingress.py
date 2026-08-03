"""``ConflatingIngress`` — the keep-latest-per-symbol ingress buffer (ADR-0023).

The subtlest concurrency in the venue package — a reader offering ticks while a
publisher drains to the bus, conflating under backpressure — tested directly
against a real ``InMemoryBus`` and a subscribed consumer, no WebSocket. The
process boundary (the WS) is not involved; this is the buffer's own contract.
"""

import asyncio
from decimal import Decimal

from structlog.typing import EventDict

from tickwright.adapters.bus import InMemoryBus
from tickwright.domain import AggressorSide, MarketTick, MarkTick
from tickwright.observability.testing import capture_events
from tickwright.venues.hyperliquid.ingress import ConflatingIngress


def _tick(symbol: str, price: str, trade_id: str) -> MarketTick:
    return MarketTick(
        symbol=symbol,
        price=Decimal(price),
        size=Decimal("1"),
        aggressor_side=AggressorSide.BUY,
        trade_id=trade_id,
        seq=0,
        venue_trade_id=True,
        ts_event=1_700_000_000_000_000_000,
        ts_init=1_700_000_000_000_000_000,
    )


def _mark(symbol: str, price: str) -> MarkTick:
    return MarkTick(
        symbol=symbol,
        price=Decimal(price),
        ts_event=1_700_000_000_000_000_000,
        ts_init=1_700_000_000_000_000_000,
    )


def test_drains_offered_ticks_to_the_bus_then_returns_when_closed() -> None:
    """A keeping-up consumer sees every offered tick, in offer order, and the
    drain loop returns once the buffer is empty and the ingress is closed."""

    async def main() -> list[MarketTick]:
        bus = InMemoryBus()
        seen: list[MarketTick] = []

        async def record(tick: MarketTick) -> None:
            seen.append(tick)

        bus.subscribe(MarketTick, record)
        ingress = ConflatingIngress(bus=bus)
        drain = asyncio.create_task(ingress.drain())
        for tick in (_tick("BTC", "100", "1"), _tick("ETH", "50", "2"), _tick("BTC", "101", "3")):
            ingress.offer(tick)
            await asyncio.sleep(0)  # let the publisher keep up, no backpressure
        ingress.close()
        await asyncio.wait_for(drain, timeout=2)
        return seen

    seen = asyncio.run(main())
    assert [(t.symbol, t.price) for t in seen] == [
        ("BTC", Decimal("100")),
        ("ETH", Decimal("50")),
        ("BTC", Decimal("101")),
    ]


def test_close_on_an_empty_buffer_ends_the_drain() -> None:
    async def main() -> None:
        ingress = ConflatingIngress(bus=InMemoryBus())
        drain = asyncio.create_task(ingress.drain())
        await asyncio.sleep(0)  # drain parks on the wake
        ingress.close()
        await asyncio.wait_for(drain, timeout=2)

    asyncio.run(main())  # returns iff close() unblocks the parked drain


def test_a_marks_conflation_is_independent_of_the_trades_for_the_same_symbol() -> None:
    """Two market-data streams, each last-value-wins **on its own** (ADR-0023).

    They share a symbol and nothing else: a mark is not a later version of a
    trade. Keyed on the symbol alone, one BTC mark would silently swallow the
    BTC trade queued beside it — the account would stop filling against a stream
    that was arriving — and a busy symbol's marks would starve behind its
    trades. So the buffer keeps one slot per stream per symbol.

    Both are folded while the first publish is stuck, so the whole exchange
    happens under real backpressure rather than passing by draining fast.
    """

    async def main() -> tuple[list[object], list[EventDict]]:
        bus = InMemoryBus()
        seen: list[object] = []
        release = asyncio.Event()
        first_delivered = asyncio.Event()

        async def slow_consumer(event: object) -> None:
            seen.append(event)
            if len(seen) == 1:
                first_delivered.set()
                await release.wait()

        bus.subscribe(MarketTick, slow_consumer)
        bus.subscribe(MarkTick, slow_consumer)
        ingress = ConflatingIngress(bus=bus)

        with capture_events() as logs:
            drain = asyncio.create_task(ingress.drain())
            ingress.offer(_tick("ETH", "50", "0"))  # blocks the drain
            await asyncio.wait_for(first_delivered.wait(), timeout=2)
            ingress.offer(_tick("BTC", "100", "1"))
            ingress.offer(_mark("BTC", "101"))
            release.set()

            async def rest_published() -> None:
                while len(seen) < 3:
                    await asyncio.sleep(0)

            await asyncio.wait_for(rest_published(), timeout=2)
            ingress.close()
            await asyncio.wait_for(drain, timeout=2)
        return seen, [log for log in logs if log["event"] == "feed.lagged"]

    seen, lagged = asyncio.run(main())

    assert [type(event).__name__ for event in seen] == ["MarketTick", "MarketTick", "MarkTick"]
    assert lagged == []  # neither superseded the other, so nothing was dropped


def test_a_superseded_mark_is_dropped_and_named_like_any_other_stale_tick() -> None:
    """Within its own stream a mark conflates exactly as a trade does — it is a
    latest-value by nature, so an unpublished one is the most droppable thing
    the feed carries. The drop is still named, never silent."""

    async def main() -> tuple[list[MarkTick], list[EventDict]]:
        bus = InMemoryBus()
        seen: list[MarkTick] = []
        release = asyncio.Event()
        first_delivered = asyncio.Event()

        async def slow_consumer(mark: MarkTick) -> None:
            seen.append(mark)
            if len(seen) == 1:
                first_delivered.set()
                await release.wait()

        bus.subscribe(MarkTick, slow_consumer)
        ingress = ConflatingIngress(bus=bus)

        with capture_events() as logs:
            drain = asyncio.create_task(ingress.drain())
            ingress.offer(_mark("BTC", "100"))
            await asyncio.wait_for(first_delivered.wait(), timeout=2)
            ingress.offer(_mark("BTC", "101"))
            ingress.offer(_mark("BTC", "102"))  # supersedes 101 → one drop
            release.set()

            async def rest_published() -> None:
                while len(seen) < 2:
                    await asyncio.sleep(0)

            await asyncio.wait_for(rest_published(), timeout=2)
            ingress.close()
            await asyncio.wait_for(drain, timeout=2)
        return seen, [log for log in logs if log["event"] == "feed.lagged"]

    seen, lagged = asyncio.run(main())

    assert [m.price for m in seen] == [Decimal("100"), Decimal("102")]
    assert len(lagged) == 1
    # The whole record, not just the symbol: this catalog entry is the *only*
    # place a conflation drop is observable (ADR-0020), so its field set is the
    # operator contract. ``stream`` is what tells an operator which of the two
    # market-data streams thinned, and a mark carries no ``dropped_trade_id``
    # to report — pinning both is what would catch a mark drop that started
    # claiming a trade's id, or a ``stream`` silently lost.
    assert lagged[0]["symbol"] == "BTC"
    assert lagged[0]["stream"] == "MarkTick"
    assert lagged[0]["dropped_trade_id"] is None


def test_backpressure_keeps_only_the_latest_per_symbol_and_names_each_drop() -> None:
    """A stuck first publish; three more ticks arrive meanwhile. BTC 101 is
    superseded by BTC 102 (one drop → one feed.lagged), ETH 50 is kept. This is
    the slow-consumer contract of ADR-0023, exercised without a WebSocket."""

    async def main() -> tuple[list[MarketTick], list[EventDict]]:
        bus = InMemoryBus()
        seen: list[MarketTick] = []
        release = asyncio.Event()
        first_delivered = asyncio.Event()

        async def slow_consumer(tick: MarketTick) -> None:
            seen.append(tick)
            if len(seen) == 1:
                first_delivered.set()
                await release.wait()

        bus.subscribe(MarketTick, slow_consumer)
        ingress = ConflatingIngress(bus=bus)

        with capture_events() as logs:
            drain = asyncio.create_task(ingress.drain())
            ingress.offer(_tick("BTC", "100", "1"))
            await asyncio.wait_for(first_delivered.wait(), timeout=2)
            # The publish is stuck in the slow consumer; fold three more in.
            ingress.offer(_tick("BTC", "101", "2"))
            ingress.offer(_tick("BTC", "102", "3"))  # supersedes 101 → one drop
            ingress.offer(_tick("ETH", "50", "4"))
            release.set()

            async def rest_published() -> None:
                while len(seen) < 3:
                    await asyncio.sleep(0)

            await asyncio.wait_for(rest_published(), timeout=2)
            ingress.close()
            await asyncio.wait_for(drain, timeout=2)
        return seen, [log for log in logs if log["event"] == "feed.lagged"]

    seen, lagged = asyncio.run(main())
    assert [(t.symbol, t.price) for t in seen] == [
        ("BTC", Decimal("100")),
        ("BTC", Decimal("102")),
        ("ETH", Decimal("50")),
    ]
    assert len(lagged) == 1
    assert lagged[0]["symbol"] == "BTC"
    assert lagged[0]["stream"] == "MarketTick"
    assert lagged[0]["dropped_trade_id"] == "2"
